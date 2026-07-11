"""Audit artifact schema, provenance, and cross-link validation.

The three audit artifacts deliberately keep workbook facts, retrieved standards, and the
agent-facing synthesis separate.  JSON Schema proves the shape of each document; this module
adds the invariants that span records and files:

* every workbook source resolves to concrete ``cells.jsonl`` records and keeps their digest;
* fact, relation, retrieval-query, citation, statement, and limitation IDs resolve;
* standards snippets retain their content digest and query ownership; and
* brief statement types cannot blur workbook evidence with authoritative standards context.

All collectors are non-throwing for artifact defects and return stable, human-readable problem
strings.  ``raise_for_audit_validation`` and the two ``validate_*`` helpers provide the strict
boundary used by emit/verify callers.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from ..resources import SCHEMA_DIR
from .model import AuditModelError, json_sha256
from .sources import WorkbookSourceResolver

_SCHEMA_DIR = SCHEMA_DIR

_SCHEMA_FILES = {
    "audit_facts": "audit_facts.schema.json",
    "standards_context": "standards_context.schema.json",
    "audit_brief": "audit_brief.schema.json",
}
_ARTIFACT_FILES = {
    "audit_facts": "data/audit_facts.json",
    "standards_context": "data/standards_context.json",
    "audit_brief": "data/audit_brief.json",
}
_WORKPAPER_FIELDS = (
    "kind",
    "title",
    "entity",
    "period_start",
    "period_end",
    "audit_phase",
    "document_state",
    "purpose",
)
_SEVERITY_ORDER = {"low": 0, "moderate": 1, "high": 2}
_READINESS_CONTEXT_LIMITATION_CODES = {
    "framework_unknown",
    "effective_date_unknown",
    "effective_date_unverified",
}
_RELATION_ENDPOINT_TYPES = {
    "tests": ({"procedure"}, {"assertion", "control"}),
    "asserts_over": ({"assertion"}, {"account"}),
    "addresses": ({"procedure"}, {"risk"}),
    "produces": ({"procedure"}, {"result", "finding"}),
}

__all__ = [
    "AuditValidationError",
    "collect_audit_validation_problems",
    "collect_audit_facts_validation_problems",
    "collect_package_audit_validation_problems",
    "load_audit_schema",
    "raise_for_audit_validation",
    "validate_audit_bundle",
    "validate_audit_facts",
    "validate_audit_package",
]


class AuditValidationError(AuditModelError):
    """One or more audit artifacts failed schema or provenance validation."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(str(problem) for problem in problems)
        detail = "\n".join(f"- {problem}" for problem in self.problems)
        super().__init__(f"audit artifact validation failed ({len(self.problems)}):\n{detail}")


def load_audit_schema(name: str) -> dict:
    """Load one of the three repository-owned audit schemas.

    ``name`` may be a logical artifact name (``audit_facts``), its schema filename, or its
    package filename.  A fresh mapping is returned on every call so a caller cannot mutate a
    process-global validator contract.
    """
    aliases = {
        logical: logical
        for logical in _SCHEMA_FILES
    }
    aliases.update({filename: logical for logical, filename in _SCHEMA_FILES.items()})
    aliases.update({Path(rel).name: logical for logical, rel in _ARTIFACT_FILES.items()})
    logical = aliases.get(name)
    if logical is None:
        raise ValueError(f"unknown audit schema: {name!r}")
    path = _SCHEMA_DIR / _SCHEMA_FILES[logical]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise AuditValidationError([f"schema file missing: {path}"]) from e
    except json.JSONDecodeError as e:
        raise AuditValidationError([f"schema JSON parse failed: {path}: {e}"]) from e
    if not isinstance(doc, dict):
        raise AuditValidationError([f"schema is not a JSON object: {path}"])
    try:
        Draft7Validator.check_schema(doc)
    except SchemaError as e:
        raise AuditValidationError([f"invalid repository schema {path.name}: {e.message}"]) from e
    return doc


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _objects(doc: Mapping[str, object], key: str) -> list[tuple[int, Mapping[str, object]]]:
    value = doc.get(key)
    if not _is_sequence(value):
        return []
    return [(index, item) for index, item in enumerate(value) if isinstance(item, Mapping)]


def _schema_path(error) -> str:
    return "/" + "/".join(str(part) for part in error.absolute_path)


def _schema_problems(name: str, doc: object) -> list[str]:
    validator = Draft7Validator(load_audit_schema(name))
    errors = sorted(
        validator.iter_errors(doc),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
    )
    return [f"{name}.schema:{_schema_path(error)}: {error.message}" for error in errors]


def _unique_ids(
    records: list[tuple[int, Mapping[str, object]]],
    *,
    path: str,
    problems: list[str],
) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for index, record in records:
        record_id = record.get("id")
        if not isinstance(record_id, str):
            continue
        if record_id in seen and record_id not in duplicates:
            problems.append(f"{path}[{index}].id: duplicate id {record_id!r}")
            duplicates.add(record_id)
        seen.add(record_id)
    return seen


def _check_id_list(
    value: object,
    known: set[str],
    *,
    path: str,
    target: str,
    problems: list[str],
) -> None:
    if not _is_sequence(value):
        return
    for index, record_id in enumerate(value):
        if isinstance(record_id, str) and record_id not in known:
            problems.append(f"{path}[{index}]: unknown {target} id {record_id!r}")


def _check_id(
    value: object,
    known: set[str],
    *,
    path: str,
    target: str,
    problems: list[str],
) -> None:
    if isinstance(value, str) and value not in known:
        problems.append(f"{path}: unknown {target} id {value!r}")


def _check_audit_relation_semantics(
    facts_by_id: Mapping[str, Mapping[str, object]],
    relations: list[tuple[int, Mapping[str, object]]],
    problems: list[str],
) -> None:
    """Validate the endpoint types of relations with an unambiguous audit meaning.

    Unknown fact IDs are left to the ordinary cross-reference checks so one bad endpoint does
    not produce a second, misleading type error.  The generic relation types intentionally stay
    unconstrained because their valid endpoint sets depend on workbook context.
    """
    for index, relation in relations:
        relation_type = relation.get("type")
        endpoint_types = _RELATION_ENDPOINT_TYPES.get(relation_type)
        if endpoint_types is None:
            continue
        allowed_from, allowed_to = endpoint_types
        for field, allowed in (
            ("from_fact_id", allowed_from),
            ("to_fact_id", allowed_to),
        ):
            fact_id = relation.get(field)
            if not isinstance(fact_id, str):
                continue
            fact = facts_by_id.get(fact_id)
            if fact is None:
                continue
            actual_type = fact.get("type")
            if actual_type not in allowed:
                expected = " or ".join(repr(value) for value in sorted(allowed))
                problems.append(
                    f"audit_facts.relations[{index}].{field}: relation type "
                    f"{relation_type!r} requires fact type {expected}, got "
                    f"{actual_type!r} for {fact_id!r}"
                )


def _check_audit_facts(
    pkg: Path,
    facts: Mapping[str, object],
    problems: list[str],
) -> dict[str, object]:
    sources = _objects(facts, "sources")
    fact_records = _objects(facts, "facts")
    relations = _objects(facts, "relations")
    queries = _objects(facts, "standard_queries")
    limitations = _objects(facts, "limitations")

    source_ids = _unique_ids(sources, path="audit_facts.sources", problems=problems)
    fact_ids = _unique_ids(fact_records, path="audit_facts.facts", problems=problems)
    facts_by_id = {
        record.get("id"): record
        for _, record in fact_records
        if isinstance(record.get("id"), str)
    }
    relation_ids = _unique_ids(
        relations, path="audit_facts.relations", problems=problems
    )
    query_ids = _unique_ids(
        queries, path="audit_facts.standard_queries", problems=problems
    )
    limitation_ids = _unique_ids(
        limitations, path="audit_facts.limitations", problems=problems
    )

    workpaper = facts.get("workpaper")
    if isinstance(workpaper, Mapping):
        _check_id_list(
            workpaper.get("source_ids"), source_ids,
            path="audit_facts.workpaper.source_ids", target="source", problems=problems,
        )

    for index, fact in fact_records:
        _check_id_list(
            fact.get("source_ids"), source_ids,
            path=f"audit_facts.facts[{index}].source_ids", target="source",
            problems=problems,
        )

    for index, relation in relations:
        _check_id(
            relation.get("from_fact_id"), fact_ids,
            path=f"audit_facts.relations[{index}].from_fact_id", target="fact",
            problems=problems,
        )
        _check_id(
            relation.get("to_fact_id"), fact_ids,
            path=f"audit_facts.relations[{index}].to_fact_id", target="fact",
            problems=problems,
        )
        _check_id_list(
            relation.get("source_ids"), source_ids,
            path=f"audit_facts.relations[{index}].source_ids", target="source",
            problems=problems,
        )
    _check_audit_relation_semantics(facts_by_id, relations, problems)

    for index, query in queries:
        _check_id_list(
            query.get("fact_ids"), fact_ids,
            path=f"audit_facts.standard_queries[{index}].fact_ids", target="fact",
            problems=problems,
        )

    for index, limitation in limitations:
        _check_id_list(
            limitation.get("affected_fact_ids"), fact_ids,
            path=f"audit_facts.limitations[{index}].affected_fact_ids", target="fact",
            problems=problems,
        )
        _check_id_list(
            limitation.get("source_ids"), source_ids,
            path=f"audit_facts.limitations[{index}].source_ids", target="source",
            problems=problems,
        )

    resolver: WorkbookSourceResolver | None
    try:
        resolver = WorkbookSourceResolver(pkg)
    except AuditModelError as e:
        resolver = None
        problems.append(f"audit_facts.sources: workbook ledger unavailable: {e}")
    if resolver is not None:
        facts_source = facts.get("source")
        meta_source = resolver.meta.get("source")
        if isinstance(facts_source, Mapping) and isinstance(meta_source, Mapping):
            if facts_source.get("sha256") != meta_source.get("sha256"):
                problems.append(
                    "audit_facts.source.sha256: differs from package meta.source.sha256"
                )
        for index, source in sources:
            sheet = source.get("sheet")
            cell_range = source.get("range")
            if not isinstance(sheet, str) or not isinstance(cell_range, str):
                continue
            ref = f"{sheet}!{cell_range}"
            try:
                resolved = resolver.resolve(ref)
            except AuditModelError as e:
                problems.append(f"audit_facts.sources[{index}]: {e}")
                continue
            digest = source.get("content_sha256")
            if isinstance(digest, str) and digest.lower() != resolved.content_sha256:
                problems.append(
                    f"audit_facts.sources[{index}].content_sha256: digest mismatch for {ref}"
                )

    return {
        "source_ids": source_ids,
        "fact_ids": fact_ids,
        "relation_ids": relation_ids,
        "query_ids": query_ids,
        "limitation_ids": limitation_ids,
        "queries": {record.get("id"): record for _, record in queries
                    if isinstance(record.get("id"), str)},
        "facts": facts_by_id,
        "workpaper": workpaper if isinstance(workpaper, Mapping) else {},
        "limitations": {record.get("id"): record for _, record in limitations
                        if isinstance(record.get("id"), str)},
        "unresolved_open_item_ids": {
            record.get("id")
            for _, record in fact_records
            if isinstance(record.get("id"), str)
            and record.get("type") == "open_item"
            and record.get("status") == "unresolved"
        },
    }


def _same_id_set(left: object, right: object) -> bool:
    if not _is_sequence(left) or not _is_sequence(right):
        return False
    return {item for item in left if isinstance(item, str)} == {
        item for item in right if isinstance(item, str)
    }


def _is_later_iso_date(candidate: object, target: object) -> bool:
    if not isinstance(candidate, str) or not isinstance(target, str):
        return False
    try:
        return date.fromisoformat(candidate) > date.fromisoformat(target)
    except ValueError:
        # JSON Schema reports malformed date strings; cross-link collection stays non-throwing.
        return False


def _check_standards_context(
    context: Mapping[str, object],
    facts_state: Mapping[str, object],
    problems: list[str],
) -> dict[str, object]:
    queries = _objects(context, "queries")
    citations = _objects(context, "citations")
    limitations = _objects(context, "limitations")
    query_ids = _unique_ids(queries, path="standards_context.queries", problems=problems)
    citation_ids = _unique_ids(
        citations, path="standards_context.citations", problems=problems
    )
    limitation_ids = _unique_ids(
        limitations, path="standards_context.limitations", problems=problems
    )
    fact_ids = set(facts_state.get("fact_ids", set()))
    planned_queries = facts_state.get("queries", {})
    if not isinstance(planned_queries, Mapping):
        planned_queries = {}

    query_by_id = {
        query.get("id"): query
        for _, query in queries
        if isinstance(query.get("id"), str)
    }
    citation_by_id = {
        citation.get("id"): citation
        for _, citation in citations
        if isinstance(citation.get("id"), str)
    }
    retriever = context.get("retriever")
    if not isinstance(retriever, Mapping):
        retriever = {}

    for planned_id in sorted(set(planned_queries) - query_ids):
        problems.append(
            f"standards_context.queries: missing result for audit_facts query {planned_id!r}"
        )

    for index, query in queries:
        query_id = query.get("id")
        if isinstance(query_id, str) and query_id not in planned_queries:
            problems.append(
                f"standards_context.queries[{index}].id: unknown audit_facts query id "
                f"{query_id!r}"
            )
        plan = planned_queries.get(query_id) if isinstance(planned_queries, Mapping) else None
        if isinstance(plan, Mapping):
            for field in (
                "query", "domain", "framework", "effective_date"
            ):
                if query.get(field) != plan.get(field):
                    problems.append(
                        f"standards_context.queries[{index}].{field}: differs from "
                        f"audit_facts.standard_queries[{query_id!r}]"
                    )
            if not _same_id_set(
                query.get("standard_nos", []), plan.get("standard_nos", [])
            ):
                problems.append(
                    f"standards_context.queries[{index}].standard_nos: differs from "
                    f"audit_facts.standard_queries[{query_id!r}]"
                )
            if not _same_id_set(query.get("fact_ids"), plan.get("fact_ids")):
                problems.append(
                    f"standards_context.queries[{index}].fact_ids: differs from "
                    f"audit_facts.standard_queries[{query_id!r}]"
                )
        _check_id_list(
            query.get("fact_ids"), fact_ids,
            path=f"standards_context.queries[{index}].fact_ids", target="fact",
            problems=problems,
        )
        _check_id_list(
            query.get("citation_ids"), citation_ids,
            path=f"standards_context.queries[{index}].citation_ids", target="citation",
            problems=problems,
        )
        matches = query.get("matches")
        match_citation_ids: list[object] = []
        match_ranks: list[int] = []
        if _is_sequence(matches):
            for match_index, match in enumerate(matches):
                if not isinstance(match, Mapping):
                    match_citation_ids.append(None)
                    continue
                match_citation_id = match.get("citation_id")
                match_citation_ids.append(match_citation_id)
                _check_id(
                    match_citation_id, citation_ids,
                    path=(
                        f"standards_context.queries[{index}].matches[{match_index}]"
                        ".citation_id"
                    ),
                    target="citation", problems=problems,
                )
                rank = match.get("rank")
                if isinstance(rank, int) and not isinstance(rank, bool):
                    match_ranks.append(rank)
            if len(match_ranks) > 1 and any(
                current <= previous
                for previous, current in zip(match_ranks, match_ranks[1:])
            ):
                problems.append(
                    f"standards_context.queries[{index}].matches: rank must be "
                    "strictly increasing in retrieval order"
                )
        result_citation_ids = query.get("citation_ids")
        if (
            _is_sequence(result_citation_ids)
            and _is_sequence(matches)
            and list(result_citation_ids) != match_citation_ids
        ):
            problems.append(
                f"standards_context.queries[{index}].citation_ids: must exactly equal "
                "matches[].citation_id in order"
            )
        if _is_sequence(query.get("citation_ids")):
            for citation_id in query.get("citation_ids", []):
                citation = citation_by_id.get(citation_id)
                owners = citation.get("query_ids") if isinstance(citation, Mapping) else None
                if isinstance(citation, Mapping) and (
                    not _is_sequence(owners) or query_id not in owners
                ):
                    problems.append(
                        f"standards_context.queries[{index}].citation_ids: citation "
                        f"{citation_id!r} does not list query {query_id!r} as an owner"
                    )

    for index, citation in citations:
        owner_ids = citation.get("query_ids")
        _check_id_list(
            owner_ids, query_ids,
            path=f"standards_context.citations[{index}].query_ids", target="query",
            problems=problems,
        )
        for query_id in owner_ids if _is_sequence(owner_ids) else []:
            owner = query_by_id.get(query_id)
            result_citation_ids = owner.get("citation_ids") if isinstance(owner, Mapping) else None
            if _is_sequence(result_citation_ids) and citation.get("id") not in result_citation_ids:
                problems.append(
                    f"standards_context.citations[{index}].id: citation is not listed by "
                    f"its query {query_id!r}"
                )
            if not isinstance(owner, Mapping):
                continue
            if citation.get("domain") != owner.get("domain"):
                problems.append(
                    f"standards_context.citations[{index}].domain: differs from "
                    f"owning query {query_id!r}"
                )
            owner_framework = owner.get("framework")
            if (
                isinstance(owner_framework, str)
                and citation.get("framework") != owner_framework
            ):
                problems.append(
                    f"standards_context.citations[{index}].framework: differs from "
                    f"owning query {query_id!r}"
                )
            if _is_later_iso_date(
                citation.get("effective_date"), owner.get("effective_date")
            ):
                problems.append(
                    f"standards_context.citations[{index}].effective_date: later than "
                    f"owning query {query_id!r} effective_date"
                )
        for citation_field, retriever_field in (
            ("corpus_id", "corpus_id"),
            ("corpus_version", "corpus_version"),
            ("retriever_version", "version"),
        ):
            if citation.get(citation_field) != retriever.get(retriever_field):
                problems.append(
                    f"standards_context.citations[{index}].{citation_field}: differs from "
                    f"standards_context.retriever.{retriever_field}"
                )
        snippet = citation.get("snippet")
        digest = citation.get("snippet_sha256")
        if isinstance(snippet, str) and isinstance(digest, str):
            expected = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
            if digest.lower() != expected:
                problems.append(
                    f"standards_context.citations[{index}].snippet_sha256: digest mismatch"
                )

    for index, limitation in limitations:
        _check_id_list(
            limitation.get("query_ids"), query_ids,
            path=f"standards_context.limitations[{index}].query_ids", target="query",
            problems=problems,
        )
        _check_id_list(
            limitation.get("citation_ids"), citation_ids,
            path=f"standards_context.limitations[{index}].citation_ids", target="citation",
            problems=problems,
        )

    return {
        "query_ids": query_ids,
        "citation_ids": citation_ids,
        "limitation_ids": limitation_ids,
        "queries": query_by_id,
        "limitations": {record.get("id"): record for _, record in limitations
                        if isinstance(record.get("id"), str)},
    }


def _statement_source_separation(
    statement: Mapping[str, object],
    *,
    path: str,
    problems: list[str],
) -> None:
    statement_type = statement.get("type")
    fact_ids = statement.get("fact_ids")
    citation_ids = statement.get("standard_citation_ids")
    fact_count = len(fact_ids) if _is_sequence(fact_ids) else 0
    citation_count = len(citation_ids) if _is_sequence(citation_ids) else 0
    status = statement.get("status")

    if statement_type == "documented_fact":
        valid = fact_count > 0 and citation_count == 0 and status == "documented"
        rule = "documented_fact requires workbook facts only and status=documented"
    elif statement_type == "authoritative_context":
        valid = fact_count == 0 and citation_count > 0 and status == "documented"
        rule = "authoritative_context requires standards citations only and status=documented"
    elif statement_type == "synthesis":
        valid = fact_count > 0 and citation_count > 0
        rule = f"{statement_type} requires both workbook facts and standards citations"
    elif statement_type == "gap":
        valid = (fact_count > 0 and citation_count > 0) or (
            fact_count == 0 and citation_count == 0
        )
        rule = "gap requires both source types, except for an empty-workbook readiness gap"
    else:
        return
    if not valid:
        problems.append(f"{path}: source separation violation: {rule}")


def _check_audit_brief(
    brief: Mapping[str, object],
    facts_state: Mapping[str, object],
    standards_state: Mapping[str, object],
    problems: list[str],
) -> dict[str, object]:
    statements = _objects(brief, "statements")
    limitations = _objects(brief, "limitations")
    statement_ids = _unique_ids(
        statements, path="audit_brief.statements", problems=problems
    )
    limitation_ids = _unique_ids(
        limitations, path="audit_brief.limitations", problems=problems
    )
    fact_ids = set(facts_state.get("fact_ids", set()))
    citation_ids = set(standards_state.get("citation_ids", set()))
    facts_limitation_ids = set(facts_state.get("limitation_ids", set()))
    standards_limitation_ids = set(standards_state.get("limitation_ids", set()))
    facts_limitations = facts_state.get("limitations", {})
    standards_limitations = standards_state.get("limitations", {})
    if not isinstance(facts_limitations, Mapping):
        facts_limitations = {}
    if not isinstance(standards_limitations, Mapping):
        standards_limitations = {}

    readiness = brief.get("readiness")
    if isinstance(readiness, Mapping):
        _check_id_list(
            readiness.get("open_item_fact_ids"), fact_ids,
            path="audit_brief.readiness.open_item_fact_ids", target="fact",
            problems=problems,
        )
        fact_by_id = facts_state.get("facts", {})
        if isinstance(fact_by_id, Mapping) and _is_sequence(readiness.get("open_item_fact_ids")):
            for index, fact_id in enumerate(readiness.get("open_item_fact_ids", [])):
                fact = fact_by_id.get(fact_id)
                if isinstance(fact, Mapping) and fact.get("type") != "open_item":
                    problems.append(
                        f"audit_brief.readiness.open_item_fact_ids[{index}]: "
                        f"fact {fact_id!r} is not type 'open_item'"
                    )
        expected_open_items = set(facts_state.get("unresolved_open_item_ids", set()))
        actual_open_items = {
            item for item in readiness.get("open_item_fact_ids", [])
            if isinstance(item, str)
        } if _is_sequence(readiness.get("open_item_fact_ids")) else set()
        for fact_id in sorted(expected_open_items - actual_open_items):
            problems.append(
                "audit_brief.readiness.open_item_fact_ids: missing unresolved "
                f"open_item fact {fact_id!r}"
            )
        for fact_id in sorted(actual_open_items - expected_open_items):
            fact = fact_by_id.get(fact_id) if isinstance(fact_by_id, Mapping) else None
            if isinstance(fact, Mapping) and fact.get("type") == "open_item":
                problems.append(
                    "audit_brief.readiness.open_item_fact_ids: includes open_item fact "
                    f"{fact_id!r} whose status is not unresolved"
                )

        query_records = standards_state.get("queries", {})
        if not isinstance(query_records, Mapping):
            query_records = {}
        has_query_error = any(
            isinstance(query, Mapping) and query.get("status") == "error"
            for query in query_records.values()
        )
        has_query_no_results = any(
            isinstance(query, Mapping) and query.get("status") == "no_results"
            for query in query_records.values()
        )
        high_limitations = [
            limitation
            for limitation in [*facts_limitations.values(), *standards_limitations.values()]
            if isinstance(limitation, Mapping) and limitation.get("severity") == "high"
        ]
        extraction_incomplete = any(
            isinstance(limitation, Mapping)
            and limitation.get("code") == "extraction_incomplete"
            for limitation in facts_limitations.values()
        )
        unverified_standards_context = any(
            isinstance(limitation, Mapping)
            and limitation.get("code") in _READINESS_CONTEXT_LIMITATION_CODES
            for limitation in standards_limitations.values()
        )
        no_facts = not fact_ids
        readiness_blockers = []
        if no_facts:
            readiness_blockers.append("no workbook facts were extracted")
        if has_query_error:
            readiness_blockers.append("a standards query failed")
        if has_query_no_results:
            readiness_blockers.append("a standards query returned no results")
        if high_limitations:
            readiness_blockers.append("high-severity input limitations remain")
        if extraction_incomplete:
            readiness_blockers.append("workbook fact extraction is incomplete")
        if unverified_standards_context:
            readiness_blockers.append(
                "standards framework or effective date remains unverified"
            )
        if expected_open_items:
            readiness_blockers.append("unresolved open items remain")
        if readiness.get("status") == "ready" and readiness_blockers:
            problems.append(
                "audit_brief.readiness.status: cannot be 'ready' while "
                + ", ".join(readiness_blockers)
            )
        if no_facts and readiness.get("status") != "not_ready":
            problems.append(
                "audit_brief.readiness.status: must be 'not_ready' when no workbook "
                "facts were extracted"
            )

    workpaper = brief.get("workpaper")
    if isinstance(workpaper, Mapping):
        _check_id_list(
            workpaper.get("fact_ids"), fact_ids,
            path="audit_brief.workpaper.fact_ids", target="fact", problems=problems,
        )
        facts_workpaper = facts_state.get("workpaper", {})
        if isinstance(facts_workpaper, Mapping):
            for field in _WORKPAPER_FIELDS:
                if workpaper.get(field) != facts_workpaper.get(field):
                    problems.append(
                        f"audit_brief.workpaper.{field}: differs from "
                        "audit_facts.workpaper"
                    )

    summary = brief.get("summary")
    if isinstance(summary, Mapping):
        _check_id_list(
            summary.get("statement_ids"), statement_ids,
            path="audit_brief.summary.statement_ids", target="statement",
            problems=problems,
        )

    for index, statement in statements:
        _check_id_list(
            statement.get("fact_ids"), fact_ids,
            path=f"audit_brief.statements[{index}].fact_ids", target="fact",
            problems=problems,
        )
        _check_id_list(
            statement.get("standard_citation_ids"), citation_ids,
            path=f"audit_brief.statements[{index}].standard_citation_ids",
            target="citation", problems=problems,
        )
        _statement_source_separation(
            statement, path=f"audit_brief.statements[{index}]", problems=problems
        )
        if (
            statement.get("type") == "gap"
            and not statement.get("fact_ids")
            and not statement.get("standard_citation_ids")
            and fact_ids
        ):
            problems.append(
                f"audit_brief.statements[{index}]: source-free gap is allowed only "
                "when no workbook facts were extracted"
            )

    for index, limitation in limitations:
        _check_id_list(
            limitation.get("audit_facts_limitation_ids"), facts_limitation_ids,
            path=f"audit_brief.limitations[{index}].audit_facts_limitation_ids",
            target="audit_facts limitation", problems=problems,
        )
        _check_id_list(
            limitation.get("standards_context_limitation_ids"), standards_limitation_ids,
            path=f"audit_brief.limitations[{index}].standards_context_limitation_ids",
            target="standards_context limitation", problems=problems,
        )
        _check_id_list(
            limitation.get("affected_statement_ids"), statement_ids,
            path=f"audit_brief.limitations[{index}].affected_statement_ids",
            target="statement", problems=problems,
        )

        linked_severities = [
            source.get("severity")
            for source_id in limitation.get("audit_facts_limitation_ids", [])
            if isinstance(source_id, str)
            and isinstance((source := facts_limitations.get(source_id)), Mapping)
        ] + [
            source.get("severity")
            for source_id in limitation.get("standards_context_limitation_ids", [])
            if isinstance(source_id, str)
            and isinstance((source := standards_limitations.get(source_id)), Mapping)
        ]
        linked_severities = [
            severity for severity in linked_severities if severity in _SEVERITY_ORDER
        ]
        if (
            linked_severities
            and limitation.get("severity") in _SEVERITY_ORDER
            and _SEVERITY_ORDER[limitation["severity"]]
            < max(_SEVERITY_ORDER[severity] for severity in linked_severities)
        ):
            problems.append(
                f"audit_brief.limitations[{index}].severity: lower than linked input "
                "limitation severity"
            )

    covered_facts_limitations = {
        limitation_id
        for _, limitation in limitations
        for limitation_id in limitation.get("audit_facts_limitation_ids", [])
        if isinstance(limitation_id, str)
    }
    covered_standards_limitations = {
        limitation_id
        for _, limitation in limitations
        for limitation_id in limitation.get("standards_context_limitation_ids", [])
        if isinstance(limitation_id, str)
    }
    for limitation_id in sorted(facts_limitation_ids - covered_facts_limitations):
        problems.append(
            "audit_brief.limitations: missing audit_facts input limitation "
            f"{limitation_id!r}"
        )
    for limitation_id in sorted(
        standards_limitation_ids - covered_standards_limitations
    ):
        problems.append(
            "audit_brief.limitations: missing standards_context input limitation "
            f"{limitation_id!r}"
        )

    if not fact_ids and not any(
        statement.get("type") == "gap" for _, statement in statements
    ):
        problems.append(
            "audit_brief.statements: a gap statement is required when no workbook "
            "facts were extracted"
        )

    return {"statement_ids": statement_ids, "limitation_ids": limitation_ids}


def _check_cross_document_inputs(
    facts: Mapping[str, object],
    context: Mapping[str, object],
    brief: Mapping[str, object],
    problems: list[str],
) -> None:
    facts_source = facts.get("source")
    context_input = context.get("input")
    brief_inputs = brief.get("inputs")
    if not all(isinstance(value, Mapping) for value in (facts_source, context_input, brief_inputs)):
        return
    workbook_values = {
        "audit_facts.source.sha256": facts_source.get("sha256"),
        "standards_context.input.workbook_sha256": context_input.get("workbook_sha256"),
        "audit_brief.inputs.workbook_sha256": brief_inputs.get("workbook_sha256"),
    }
    valid_workbook_values = {value for value in workbook_values.values() if isinstance(value, str)}
    if len(valid_workbook_values) > 1:
        problems.append(
            "audit_bundle.workbook_sha256: workbook digest differs across artifacts: "
            + repr(workbook_values)
        )
    context_facts_sha = context_input.get("audit_facts_sha256")
    brief_facts_sha = brief_inputs.get("audit_facts_sha256")
    actual_facts_sha = json_sha256(dict(facts))
    actual_context_sha = json_sha256(dict(context))
    if isinstance(context_facts_sha, str) and context_facts_sha != actual_facts_sha:
        problems.append(
            "standards_context.input.audit_facts_sha256: does not match audit_facts content"
        )
    if isinstance(brief_facts_sha, str) and brief_facts_sha != actual_facts_sha:
        problems.append(
            "audit_brief.inputs.audit_facts_sha256: does not match audit_facts content"
        )
    brief_context_sha = brief_inputs.get("standards_context_sha256")
    if isinstance(brief_context_sha, str) and brief_context_sha != actual_context_sha:
        problems.append(
            "audit_brief.inputs.standards_context_sha256: does not match standards_context content"
        )
    if (
        isinstance(context_facts_sha, str)
        and isinstance(brief_facts_sha, str)
        and context_facts_sha != brief_facts_sha
    ):
        problems.append(
            "audit_brief.inputs.audit_facts_sha256: differs from "
            "standards_context.input.audit_facts_sha256"
        )


def _check_global_id_namespace(
    facts: Mapping[str, object],
    context: Mapping[str, object],
    brief: Mapping[str, object],
    problems: list[str],
) -> None:
    """소비 API의 전역 ID 조회가 모호해지지 않도록 registry 간 충돌을 막는다."""
    groups = (
        ("audit_facts.sources", facts, "sources"),
        ("audit_facts.facts", facts, "facts"),
        ("audit_facts.relations", facts, "relations"),
        ("audit_facts.standard_queries", facts, "standard_queries"),
        ("audit_facts.limitations", facts, "limitations"),
        ("standards_context.queries", context, "queries"),
        ("standards_context.citations", context, "citations"),
        ("standards_context.limitations", context, "limitations"),
        ("audit_brief.statements", brief, "statements"),
        ("audit_brief.limitations", brief, "limitations"),
    )
    locations: dict[str, list[str]] = {}
    for label, doc, key in groups:
        for index, record in _objects(doc, key):
            record_id = record.get("id")
            if isinstance(record_id, str):
                locations.setdefault(record_id, []).append(f"{label}[{index}]")
    allowed_query_pair = {
        "audit_facts.standard_queries",
        "standards_context.queries",
    }
    for record_id, paths in sorted(locations.items()):
        if len(paths) < 2:
            continue
        registries = {path.split("[", 1)[0] for path in paths}
        if len(paths) == 2 and registries == allowed_query_pair:
            continue
        problems.append(
            f"audit_bundle.ids: global id collision {record_id!r}: {paths}"
        )


def collect_audit_facts_validation_problems(
    pkg: Path | str,
    audit_facts: object,
) -> list[str]:
    """Return schema, workbook-provenance, and relation problems for facts alone."""
    pkg = Path(pkg)
    problems = _schema_problems("audit_facts", audit_facts)
    if isinstance(audit_facts, Mapping):
        _check_audit_facts(pkg, audit_facts, problems)
    return list(dict.fromkeys(problems))


def validate_audit_facts(pkg: Path | str, audit_facts: object) -> None:
    """Strict validation boundary used before standalone extraction writes an artifact."""
    raise_for_audit_validation(
        collect_audit_facts_validation_problems(pkg, audit_facts)
    )


def collect_audit_validation_problems(
    pkg: Path | str,
    audit_facts: object,
    standards_context: object,
    audit_brief: object,
) -> list[str]:
    """Return all schema, provenance, and cross-link problems for an in-memory bundle."""
    pkg = Path(pkg)
    problems: list[str] = []
    problems.extend(_schema_problems("audit_facts", audit_facts))
    problems.extend(_schema_problems("standards_context", standards_context))
    problems.extend(_schema_problems("audit_brief", audit_brief))

    facts_state: dict[str, object] = {
        "fact_ids": set(), "limitation_ids": set(), "queries": {}, "facts": {},
        "workpaper": {}, "limitations": {}, "unresolved_open_item_ids": set(),
    }
    standards_state: dict[str, object] = {
        "citation_ids": set(), "limitation_ids": set(), "queries": {},
        "limitations": {},
    }
    if isinstance(audit_facts, Mapping):
        facts_state = _check_audit_facts(pkg, audit_facts, problems)
    if isinstance(standards_context, Mapping):
        standards_state = _check_standards_context(
            standards_context, facts_state, problems
        )
    if isinstance(audit_brief, Mapping):
        _check_audit_brief(audit_brief, facts_state, standards_state, problems)
    if (
        isinstance(audit_facts, Mapping)
        and isinstance(standards_context, Mapping)
        and isinstance(audit_brief, Mapping)
    ):
        _check_cross_document_inputs(audit_facts, standards_context, audit_brief, problems)
        _check_global_id_namespace(
            audit_facts, standards_context, audit_brief, problems
        )

    # A malformed document can trigger the same semantic problem through two reciprocal links.
    # Keep the first occurrence while preserving deterministic traversal order.
    return list(dict.fromkeys(problems))


def _read_artifact(pkg: Path, logical: str) -> tuple[dict | None, str | None]:
    rel = _ARTIFACT_FILES[logical]
    path = pkg / rel
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"{rel}: file missing"
    except json.JSONDecodeError as e:
        return None, f"{rel}: JSON parse failed: {e}"
    except OSError as e:
        return None, f"{rel}: read failed: {e}"
    if not isinstance(doc, dict):
        return None, f"{rel}: artifact is not a JSON object"
    return doc, None


def collect_package_audit_validation_problems(pkg: Path | str) -> list[str]:
    """Load the fixed package paths and return bundle validation problems."""
    pkg = Path(pkg)
    docs: dict[str, dict] = {}
    problems: list[str] = []
    for logical in _ARTIFACT_FILES:
        doc, problem = _read_artifact(pkg, logical)
        if problem is not None:
            problems.append(problem)
        elif doc is not None:
            docs[logical] = doc
    if problems:
        return problems
    return collect_audit_validation_problems(
        pkg,
        docs["audit_facts"],
        docs["standards_context"],
        docs["audit_brief"],
    )


def raise_for_audit_validation(problems: Sequence[str]) -> None:
    """Raise ``AuditValidationError`` when a collected problem list is non-empty."""
    if problems:
        raise AuditValidationError(problems)


def validate_audit_bundle(
    pkg: Path | str,
    audit_facts: object,
    standards_context: object,
    audit_brief: object,
) -> None:
    """Strict in-memory counterpart of ``collect_audit_validation_problems``."""
    raise_for_audit_validation(
        collect_audit_validation_problems(pkg, audit_facts, standards_context, audit_brief)
    )


def validate_audit_package(pkg: Path | str) -> None:
    """Strict fixed-path package validation helper."""
    raise_for_audit_validation(collect_package_audit_validation_problems(pkg))
