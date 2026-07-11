"""Agent-facing deterministic readers for prepared audit bundles."""
from __future__ import annotations

import json
from pathlib import Path

from .contract import PREPARE_VERSION, bundle_keys
from .model import AuditModelError
from .sources import WorkbookSourceResolver
from .validate import AuditValidationError, validate_audit_bundle


DEFAULT_LIMIT = 100
HARD_LIMIT = 2000
_ARTIFACT_RELS = (
    "data/audit_facts.json",
    "data/standards_context.json",
    "data/audit_brief.json",
)
_COMMIT_FIELDS = {
    "present",
    "status",
    "version",
    "facts_key",
    "standards_key",
    "brief_key",
    "prepared_at",
    "review_status",
}


class AuditConsumeError(RuntimeError):
    """A prepared audit bundle is missing, damaged, or queried incorrectly."""


def _limit(value, default: int = DEFAULT_LIMIT) -> int:
    if value is None:
        return default
    try:
        value = int(value)
    except (TypeError, ValueError) as e:
        raise AuditConsumeError(f"limit은 정수여야 합니다: {value!r}") from e
    return max(1, min(value, HARD_LIMIT))


def _load(pkg: Path, rel: str) -> dict:
    path = pkg / rel
    if not path.is_file():
        raise AuditConsumeError(f"{rel} 없음 — prepare가 완료된 패키지가 아닙니다.")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise AuditConsumeError(f"{rel} JSON 파싱 실패: {e}") from e
    if not isinstance(doc, dict):
        raise AuditConsumeError(f"{rel}은 JSON 객체여야 합니다.")
    return doc


def load_validated_audit_bundle(
    pkg: Path | str,
    *,
    allow_absent: bool = False,
) -> tuple[Path, dict, dict, dict] | None:
    """Load the committed audit bundle after integrity and provenance validation.

    The three JSON files are staging material until ``meta.audit_preparation`` advertises the
    exact validated keys.  Keeping this boundary in the reader prevents a failed/interrupted
    prepare, manual file copy, or later artifact edit from becoming agent-visible content.
    ``allow_absent`` lets legacy SKILL/overview consumers remain usable when no audit artifacts
    or commit marker exist; any partial presence is still rejected.
    """
    path = Path(pkg)
    meta = _load(path, "meta.json")
    audit_meta = meta.get("audit_preparation")
    artifacts_present = [rel for rel in _ARTIFACT_RELS if (path / rel).is_file()]
    if audit_meta is None and not artifacts_present:
        if allow_absent:
            return None
        raise AuditConsumeError("감사 prepare 완료 표식이 없습니다.")
    if not isinstance(audit_meta, dict) or audit_meta.get("present") is not True:
        raise AuditConsumeError(
            "meta.audit_preparation 완료 표식이 유효하지 않습니다."
        )
    missing_fields = sorted(_COMMIT_FIELDS - set(audit_meta))
    if missing_fields:
        raise AuditConsumeError(
            f"meta.audit_preparation 완료 표식 필드 누락: {missing_fields}"
        )
    if not isinstance(audit_meta.get("prepared_at"), str):
        raise AuditConsumeError("meta.audit_preparation.prepared_at이 유효하지 않습니다.")

    facts, context, brief_doc = (
        _load(path, rel) for rel in _ARTIFACT_RELS
    )
    try:
        validate_audit_bundle(path, facts, context, brief_doc)
    except AuditValidationError as e:
        detail = "; ".join(e.problems[:5])
        suffix = " ..." if len(e.problems) > 5 else ""
        raise AuditConsumeError(
            f"감사 bundle 검증 실패({len(e.problems)}건): {detail}{suffix}"
        ) from e

    try:
        keys = bundle_keys(facts, context, brief_doc)
    except (AuditModelError, TypeError, ValueError) as e:
        raise AuditConsumeError(f"감사 bundle key 계산 실패: {e}") from e
    advertised_keys = tuple(
        audit_meta.get(name) for name in ("facts_key", "standards_key", "brief_key")
    )
    if advertised_keys != keys:
        raise AuditConsumeError("meta.audit_preparation artifact key가 일치하지 않습니다.")
    if audit_meta.get("version") != PREPARE_VERSION:
        raise AuditConsumeError(
            "meta.audit_preparation.version이 현재 prepare 계약과 일치하지 않습니다."
        )
    readiness_status = brief_doc.get("readiness", {}).get("status")
    if audit_meta.get("status") != readiness_status:
        raise AuditConsumeError(
            "meta.audit_preparation.status가 audit_brief.readiness와 일치하지 않습니다."
        )
    review_status = brief_doc.get("review", {}).get("status")
    if audit_meta.get("review_status") != review_status:
        raise AuditConsumeError(
            "meta.audit_preparation.review_status가 audit_brief.review와 일치하지 않습니다."
        )
    return path, facts, context, brief_doc


def _bundle(pkg: Path | str) -> tuple[Path, dict, dict, dict]:
    loaded = load_validated_audit_bundle(pkg)
    assert loaded is not None  # allow_absent=False
    return loaded


def _trust_marker(brief_doc: dict) -> dict:
    review_status = brief_doc.get("review", {}).get("status")
    return {
        "review_status": review_status,
        "unreviewed": review_status != "approved",
    }


def brief(pkg: Path | str, *, limit=None) -> dict:
    """Return the prepared brief, including draft content with an explicit trust marker."""
    _, facts, context, doc = _bundle(pkg)
    lim = _limit(limit)
    statements = doc.get("statements", [])
    return {
        "schema_version": doc.get("schema_version"),
        **_trust_marker(doc),
        "readiness": doc.get("readiness"),
        "workpaper": doc.get("workpaper"),
        "summary": doc.get("summary"),
        "returned": min(len(statements), lim),
        "total_statements": len(statements),
        "truncated": len(statements) > lim,
        "statements": statements[:lim],
        "limitations": doc.get("limitations", []),
        "counts": {
            "facts": len(facts.get("facts", [])),
            "relations": len(facts.get("relations", [])),
            "standards_citations": len(context.get("citations", [])),
        },
    }


def audit_search(
    pkg: Path | str,
    *,
    query: str,
    kind: str | None = None,
    limit=None,
) -> dict:
    """Search normalized facts and brief statements, not the raw cell ledger."""
    if not isinstance(query, str) or not query.strip():
        raise AuditConsumeError("query가 비어 있습니다.")
    _, facts, _, brief_doc = _bundle(pkg)
    q = query.casefold()
    matches: list[dict] = []
    for fact in facts.get("facts", []):
        if kind is not None and kind not in ("fact", fact.get("type")):
            continue
        haystack = "\n".join(
            str(value)
            for value in (
                fact.get("description"), fact.get("normalized_code"), fact.get("value")
            )
            if value is not None
        ).casefold()
        if q in haystack:
            matches.append({"kind": "fact", "item": fact})
    for statement in brief_doc.get("statements", []):
        if kind is not None and kind not in (
            "statement", statement.get("type"), statement.get("section")
        ):
            continue
        if q in str(statement.get("text", "")).casefold():
            matches.append({"kind": "statement", "item": statement})
    lim = _limit(limit)
    return {
        **_trust_marker(brief_doc),
        "query": query,
        "kind": kind,
        "returned": min(len(matches), lim),
        "total_matches": len(matches),
        "truncated": len(matches) > lim,
        "matches": matches[:lim],
    }


def _fact_matches(fact: dict, query: str | None) -> bool:
    if query is None:
        return True
    haystack = "\n".join(
        str(value)
        for value in (
            fact.get("type"),
            fact.get("description"),
            fact.get("normalized_code"),
            fact.get("value"),
            fact.get("status"),
        )
        if value is not None
    ).casefold()
    return query in haystack


def _unique_ids(values) -> list[str]:
    return list(dict.fromkeys(
        value for value in values if isinstance(value, str) and value
    ))


def _bounded_assertion_pair(pair: dict, limit: int) -> dict:
    """Apply the public list budget to every collection nested in one pair."""
    test_relations = pair["test_relations"]
    produced_facts = pair["produced_facts"]
    produces_relations = pair["produces_relations"]
    trace_ids = pair["trace_ids"]
    test_relations_truncated = len(test_relations) > limit
    produced_facts_truncated = len(produced_facts) > limit
    produces_relations_truncated = len(produces_relations) > limit
    trace_ids_truncated = len(trace_ids) > limit
    return {
        "assertion": pair["assertion"],
        "procedure": pair["procedure"],
        "mapping_status": pair["mapping_status"],
        "returned_test_relations": min(len(test_relations), limit),
        "total_test_relations": len(test_relations),
        "test_relations_truncated": test_relations_truncated,
        "test_relations": test_relations[:limit],
        "returned_produced_facts": min(len(produced_facts), limit),
        "total_produced_facts": len(produced_facts),
        "produced_facts_truncated": produced_facts_truncated,
        "produced_facts": produced_facts[:limit],
        "returned_produces_relations": min(len(produces_relations), limit),
        "total_produces_relations": len(produces_relations),
        "produces_relations_truncated": produces_relations_truncated,
        "produces_relations": produces_relations[:limit],
        "returned_trace_ids": min(len(trace_ids), limit),
        "total_trace_ids": len(trace_ids),
        "trace_ids_truncated": trace_ids_truncated,
        "trace_ids": trace_ids[:limit],
        "truncated": any((
            test_relations_truncated,
            produced_facts_truncated,
            produces_relations_truncated,
            trace_ids_truncated,
        )),
    }


def assertion_procedures(
    pkg: Path | str,
    *,
    query: str | None = None,
    limit=None,
) -> dict:
    """Return only explicitly represented procedure-to-assertion test mappings.

    A pair exists only when a ``tests`` relation points from a ``procedure`` fact to an
    ``assertion`` fact.  Reversed relations and prose similarity are deliberately ignored.
    Result/finding facts are included only through an explicit ``produces`` relation whose
    source is that procedure.  Artifact relations marked inferred remain visible, but each pair
    exposes a documented/inferred/unknown ``mapping_status`` instead of silently promoting them.
    """
    _, facts, _, brief_doc = _bundle(pkg)
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            raise AuditConsumeError("query가 비어 있습니다.")
        query_text: str | None = query.strip()
        folded_query: str | None = query_text.casefold()
    else:
        query_text = None
        folded_query = None

    fact_map = {fact["id"]: fact for fact in facts.get("facts", [])}
    assertions = sorted(
        (fact for fact in fact_map.values() if fact.get("type") == "assertion"),
        key=lambda fact: fact["id"],
    )
    procedures = sorted(
        (fact for fact in fact_map.values() if fact.get("type") == "procedure"),
        key=lambda fact: fact["id"],
    )

    tests_by_pair: dict[tuple[str, str], list[dict]] = {}
    produces_by_procedure: dict[str, list[dict]] = {}
    for relation in sorted(facts.get("relations", []), key=lambda item: item["id"]):
        from_fact = fact_map.get(relation.get("from_fact_id"))
        to_fact = fact_map.get(relation.get("to_fact_id"))
        if (
            relation.get("type") == "tests"
            and from_fact is not None
            and from_fact.get("type") == "procedure"
            and to_fact is not None
            and to_fact.get("type") == "assertion"
        ):
            key = (to_fact["id"], from_fact["id"])
            tests_by_pair.setdefault(key, []).append(relation)
        elif (
            relation.get("type") == "produces"
            and from_fact is not None
            and from_fact.get("type") == "procedure"
            and to_fact is not None
            and to_fact.get("type") in {"result", "finding"}
        ):
            produces_by_procedure.setdefault(from_fact["id"], []).append(relation)

    paired_assertion_ids: set[str] = set()
    paired_procedure_ids: set[str] = set()
    pairs: list[dict] = []
    for assertion_id, procedure_id in sorted(tests_by_pair):
        assertion = fact_map[assertion_id]
        procedure = fact_map[procedure_id]
        test_relations = tests_by_pair[(assertion_id, procedure_id)]
        produces_relations = produces_by_procedure.get(procedure_id, [])
        produced_facts = sorted(
            {
                relation["to_fact_id"]: fact_map[relation["to_fact_id"]]
                for relation in produces_relations
            }.values(),
            key=lambda fact: fact["id"],
        )
        paired_assertion_ids.add(assertion_id)
        paired_procedure_ids.add(procedure_id)
        if not any(
            _fact_matches(fact, folded_query)
            for fact in (assertion, procedure, *produced_facts)
        ):
            continue
        trace_ids = _unique_ids((
            assertion_id,
            procedure_id,
            *(relation["id"] for relation in test_relations),
            *(fact["id"] for fact in produced_facts),
            *(relation["id"] for relation in produces_relations),
        ))
        mapping_statuses = {relation.get("status") for relation in test_relations}
        if "documented" in mapping_statuses:
            mapping_status = "documented"
        elif "inferred" in mapping_statuses:
            mapping_status = "inferred"
        else:
            mapping_status = "unknown"
        pairs.append({
            "assertion": assertion,
            "procedure": procedure,
            "mapping_status": mapping_status,
            "test_relations": test_relations,
            "produced_facts": produced_facts,
            "produces_relations": produces_relations,
            "trace_ids": trace_ids,
        })

    unpaired_assertions = [
        fact for fact in assertions
        if fact["id"] not in paired_assertion_ids
        and _fact_matches(fact, folded_query)
    ]
    unpaired_procedures = [
        fact for fact in procedures
        if fact["id"] not in paired_procedure_ids
        and _fact_matches(fact, folded_query)
    ]
    lim = _limit(limit)
    full_returned_pairs = pairs[:lim]
    returned_pairs = [
        _bounded_assertion_pair(pair, lim) for pair in full_returned_pairs
    ]
    returned_assertions = unpaired_assertions[:lim]
    returned_procedures = unpaired_procedures[:lim]
    all_trace_ids = _unique_ids((
        *(
            trace_id
            for pair in full_returned_pairs
            for trace_id in pair["trace_ids"]
        ),
        *(fact["id"] for fact in returned_assertions),
        *(fact["id"] for fact in returned_procedures),
    ))
    trace_ids = all_trace_ids[:lim]
    trace_ids_truncated = len(all_trace_ids) > lim
    truncated = any((
        len(pairs) > lim,
        len(unpaired_assertions) > lim,
        len(unpaired_procedures) > lim,
        trace_ids_truncated,
        any(pair["truncated"] for pair in returned_pairs),
    ))
    return {
        **_trust_marker(brief_doc),
        "query": query_text,
        "returned_pairs": len(returned_pairs),
        "total_pairs": len(pairs),
        "documented_pairs": sum(
            pair["mapping_status"] == "documented" for pair in pairs
        ),
        "inferred_pairs": sum(
            pair["mapping_status"] == "inferred" for pair in pairs
        ),
        "unknown_pairs": sum(
            pair["mapping_status"] == "unknown" for pair in pairs
        ),
        "returned_unpaired_assertions": len(returned_assertions),
        "total_unpaired_assertions": len(unpaired_assertions),
        "returned_unpaired_procedures": len(returned_procedures),
        "total_unpaired_procedures": len(unpaired_procedures),
        "returned_trace_ids": len(trace_ids),
        "total_trace_ids": len(all_trace_ids),
        "trace_ids_truncated": trace_ids_truncated,
        "truncated": truncated,
        "pairs": returned_pairs,
        "unpaired_assertions": returned_assertions,
        "unpaired_procedures": returned_procedures,
        "trace_ids": trace_ids,
    }


def _registries(facts: dict, context: dict, brief_doc: dict) -> list[tuple[str, dict]]:
    groups = (
        ("source", facts.get("sources", [])),
        ("fact", facts.get("facts", [])),
        ("relation", facts.get("relations", [])),
        ("standard_query", facts.get("standard_queries", [])),
        ("fact_limitation", facts.get("limitations", [])),
        ("query_result", context.get("queries", [])),
        ("standard_citation", context.get("citations", [])),
        ("context_limitation", context.get("limitations", [])),
        ("statement", brief_doc.get("statements", [])),
        ("brief_limitation", brief_doc.get("limitations", [])),
    )
    return [(kind, item) for kind, items in groups for item in items]


def _get_from_bundle(
    facts: dict,
    context: dict,
    brief_doc: dict,
    *,
    item_id: str,
) -> dict:
    found = [(kind, item) for kind, item in _registries(facts, context, brief_doc)
             if item.get("id") == item_id]
    if not found:
        raise AuditConsumeError(f"audit item 없음: {item_id!r}")
    if len(found) > 1:
        by_kind = {kind: item for kind, item in found}
        if set(by_kind) == {"standard_query", "query_result"} and len(found) == 2:
            return {
                "id": item_id,
                "kind": "standard_query",
                "item": {
                    "plan": by_kind["standard_query"],
                    "result": by_kind["query_result"],
                },
            }
        raise AuditConsumeError(f"audit item ID 중복: {item_id!r}")
    kind, item = found[0]
    return {"id": item_id, "kind": kind, "item": item}


def audit_get(pkg: Path | str, *, item_id: str) -> dict:
    """Return one typed audit object by ID."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise AuditConsumeError("item_id가 비어 있습니다.")
    _, facts, context, brief_doc = _bundle(pkg)
    return {
        **_trust_marker(brief_doc),
        **_get_from_bundle(facts, context, brief_doc, item_id=item_id),
    }


def _cell_view(cell: dict) -> dict:
    out = {
        "sheet": cell.get("sheet"),
        "cell": cell.get("cell"),
        "value": cell.get("value"),
        "formula": cell.get("formula"),
    }
    for key in ("cached_value", "number_format", "merged_range"):
        if cell.get(key) is not None:
            out[key] = cell[key]
    return out


def trace(pkg: Path | str, *, item_id: str, limit=None) -> dict:
    """Resolve a fact/statement/relation to workbook cells and cached standards passages."""
    path, facts, context, brief_doc = _bundle(pkg)
    if not isinstance(item_id, str) or not item_id.strip():
        raise AuditConsumeError("item_id가 비어 있습니다.")
    located = _get_from_bundle(facts, context, brief_doc, item_id=item_id)
    item, kind = located["item"], located["kind"]
    fact_ids: list[str] = []
    citation_ids: list[str] = []
    direct_source_ids: list[str] = []
    if kind == "fact":
        fact_ids = [item_id]
    elif kind == "statement":
        fact_ids = list(item.get("fact_ids", []))
        citation_ids = list(item.get("standard_citation_ids", []))
    elif kind == "relation":
        fact_ids = [item.get("from_fact_id"), item.get("to_fact_id")]
        direct_source_ids = list(item.get("source_ids", []))
    elif kind == "standard_citation":
        citation_ids = [item_id]
    else:
        raise AuditConsumeError(
            f"trace는 fact/statement/relation/standard_citation만 지원합니다: {kind}"
        )
    fact_ids = [value for value in fact_ids if isinstance(value, str)]
    fact_map = {fact["id"]: fact for fact in facts.get("facts", [])}
    source_map = {source["id"]: source for source in facts.get("sources", [])}
    citation_map = {citation["id"]: citation for citation in context.get("citations", [])}
    missing_facts = [fid for fid in fact_ids if fid not in fact_map]
    missing_citations = [cid for cid in citation_ids if cid not in citation_map]
    if missing_facts or missing_citations:
        raise AuditConsumeError(
            "trace 참조가 손상되었습니다: "
            f"facts={missing_facts[:10]}, citations={missing_citations[:10]}"
        )
    linked_facts = [fact_map[fid] for fid in dict.fromkeys(fact_ids)]
    source_ids = list(dict.fromkeys(
        [*direct_source_ids, *(
            source_id
            for fact in linked_facts
            for source_id in fact.get("source_ids", [])
        )]
    ))
    missing_sources = [source_id for source_id in source_ids if source_id not in source_map]
    if missing_sources:
        raise AuditConsumeError(
            f"trace source 참조가 손상되었습니다: {missing_sources[:10]}"
        )
    sources = [source_map[source_id] for source_id in source_ids]
    citations = [citation_map[cid] for cid in dict.fromkeys(citation_ids)]

    try:
        resolver = WorkbookSourceResolver(path)
        cells: list[dict] = []
        for source in sources:
            ref = f"{source['sheet']}!{source['range']}"
            resolved = resolver.resolve(ref)
            if resolved.content_sha256 != source.get("content_sha256"):
                raise AuditConsumeError(f"workbook source digest 불일치: {ref}")
            cells.extend(_cell_view(cell) for cell in resolver.cells_for(ref))
    except AuditModelError as e:
        raise AuditConsumeError(str(e)) from e
    # Preserve first occurrence when overlapping cited ranges resolve the same cell.
    deduped: list[dict] = []
    seen: set[tuple[object, object]] = set()
    for cell in cells:
        key = (cell.get("sheet"), cell.get("cell"))
        if key not in seen:
            seen.add(key)
            deduped.append(cell)
    lim = _limit(limit)
    return {
        **_trust_marker(brief_doc),
        "id": item_id,
        "kind": kind,
        "item": item,
        "returned_facts": min(len(linked_facts), lim),
        "total_facts": len(linked_facts),
        "facts": linked_facts[:lim],
        "returned_sources": min(len(sources), lim),
        "total_sources": len(sources),
        "sources": sources[:lim],
        "returned_standards_citations": min(len(citations), lim),
        "total_standards_citations": len(citations),
        "standards_citations": citations[:lim],
        "returned_cells": min(len(deduped), lim),
        "total_cells": len(deduped),
        "truncated": len(deduped) > lim,
        "cells": deduped[:lim],
    }
