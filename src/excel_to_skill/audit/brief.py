"""Build the agent-ready brief from workbook facts and cached standards context."""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping

import jsonschema

from ..meta import _now_iso
from .llm import AuditLLMError, call_json, load_prompt, load_schema
from .model import json_sha256


BRIEF_VERSION = "0.5.4"  # fail closed on unknown or cross-domain statement evidence
BRIEF_PROMPT = "audit_brief_v1.md"
MAX_MODEL_STATEMENTS = 24
MAX_MODEL_LIMITATIONS = 24

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
_STANDARD_REFERENCE_RE = re.compile(
    r"\b(KSA|K-?IFRS)\s*(?:(?:::\s*|[- ]+)\s*(?:제\s*)?|제\s*)"
    r"([0-9]+(?:-[0-9]+)?)(?:\s*호)?(?=$|[^0-9A-Za-z-])",
    re.IGNORECASE,
)
_KOREAN_STANDARD_REFERENCE_PATTERNS = (
    (
        "KSA",
        re.compile(
            r"(?:감사기준서?|감사기준)\s*(?:제\s*)?"
            r"([0-9]+(?:-[0-9]+)?)(?:\s*호)?(?=$|[^0-9A-Za-z-])"
        ),
    ),
    (
        "KIFRS",
        re.compile(
            r"(?:기업회계기준서?|회계기준서|한국채택국제회계기준서?)\s*(?:제\s*)?"
            r"([0-9]+(?:-[0-9]+)?)(?:\s*호)?(?=$|[^0-9A-Za-z-])"
        ),
    ),
)


def _output_schema(full_schema: dict) -> dict:
    """Schema for only the model-authored portion of ``audit_brief.json``."""
    names = ("readiness", "workpaper", "summary", "statements", "limitations")
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": list(names),
        "properties": {
            name: copy.deepcopy(full_schema["properties"][name]) for name in names
        },
        "definitions": copy.deepcopy(full_schema["definitions"]),
    }
    # Large audit-program templates may contain dozens of near-repeated procedures.  The model
    # must synthesize them into a brief instead of attempting a one-statement-per-fact mirror
    # that can truncate a forced tool call before its required fields are emitted.  These are
    # provider-output budgets only; deterministic integrity completion may add relation endpoints
    # and upstream limitations before the full artifact schema is validated.
    schema["properties"]["statements"]["maxItems"] = MAX_MODEL_STATEMENTS
    schema["properties"]["limitations"]["maxItems"] = MAX_MODEL_LIMITATIONS
    return schema


def _records(doc: Mapping[str, object], key: str) -> list[dict]:
    value = doc.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _pre_integrity_schema(provider_schema: dict) -> dict:
    """Retain shape validation while moving source-separation normalization into code."""
    schema = copy.deepcopy(provider_schema)
    schema["definitions"]["briefStatement"].pop("allOf", None)
    schema["definitions"]["briefStatement"]["properties"]["status"] = {
        "type": "string",
        "minLength": 1,
    }
    schema["definitions"]["briefLimitation"].pop("anyOf", None)
    return schema


def _known_ids(values: object, known: set[str]) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        value for value in values
        if isinstance(value, str) and value in known
    ))


def _empty_workbook_statement() -> dict:
    return {
        "id": "statement:empty_workbook",
        "section": "open_items",
        "type": "gap",
        "text": (
            "No workbook facts were extracted; substantive audit analysis is not "
            "ready from the extracted workbook content."
        ),
        "status": "unknown",
        "confidence": 1.0,
        "fact_ids": [],
        "relation_ids": [],
        "standard_citation_ids": [],
    }


def _normalize_authored_records(
    authored: dict,
    audit_facts: Mapping[str, object],
    standards_context: Mapping[str, object],
) -> None:
    """Treat model output as bounded selections and enforce trust domains in code.

    A statement containing an unknown ID or evidence forbidden for its declared type is dropped
    whole.  Removing only the bad ID could leave unsupported model prose attached to an unrelated
    valid ID and thereby launder the claim.  The model's prose is never rewritten into a different
    substantive claim; only conservative status, summary links, and limitation links are
    normalized before the strict final artifact schema and cross-document validator run.
    """
    facts = {
        item["id"]: item
        for item in _records(audit_facts, "facts")
        if isinstance(item.get("id"), str)
    }
    relations = {
        item["id"]: item
        for item in _records(audit_facts, "relations")
        if isinstance(item.get("id"), str)
    }
    citations = {
        item["id"]
        for item in _records(standards_context, "citations")
        if isinstance(item.get("id"), str)
    }
    normalized: list[dict] = []
    used_statement_ids: set[str] = set()
    for raw in authored.get("statements", []):
        if not isinstance(raw, dict):
            continue
        statement = copy.deepcopy(raw)
        statement_id = statement.get("id")
        if not isinstance(statement_id, str) or statement_id in used_statement_ids:
            continue
        raw_fact_ids = statement.get("fact_ids", [])
        raw_relation_ids = statement.get("relation_ids", [])
        raw_citation_ids = statement.get("standard_citation_ids", [])
        if (
            not isinstance(raw_fact_ids, list)
            or not isinstance(raw_relation_ids, list)
            or not isinstance(raw_citation_ids, list)
            or any(item not in facts for item in raw_fact_ids)
            or any(item not in relations for item in raw_relation_ids)
            or any(item not in citations for item in raw_citation_ids)
        ):
            continue
        statement["fact_ids"] = list(raw_fact_ids)
        statement["relation_ids"] = list(raw_relation_ids)
        statement["standard_citation_ids"] = list(raw_citation_ids)
        statement_type = statement.get("type")
        if statement_type == "documented_fact":
            if statement["standard_citation_ids"] or any(
                relations[relation_id].get("status") != "documented"
                for relation_id in statement["relation_ids"]
            ):
                continue
            statement["status"] = "documented"
            seen = set(statement["fact_ids"])
            for relation_id in statement["relation_ids"]:
                relation = relations[relation_id]
                for field in ("from_fact_id", "to_fact_id"):
                    fact_id = relation.get(field)
                    if isinstance(fact_id, str) and fact_id in facts and fact_id not in seen:
                        statement["fact_ids"].append(fact_id)
                        seen.add(fact_id)
            if not statement["fact_ids"]:
                continue
        elif statement_type == "authoritative_context":
            if statement["fact_ids"] or statement["relation_ids"]:
                continue
            statement["status"] = "documented"
            if not statement["standard_citation_ids"]:
                continue
        elif statement_type == "synthesis":
            if not statement["fact_ids"] or not statement["standard_citation_ids"]:
                continue
        elif statement_type == "gap":
            has_facts = bool(statement["fact_ids"])
            has_standards = bool(statement["standard_citation_ids"])
            if not (has_facts and has_standards):
                if facts or has_facts or has_standards:
                    continue
                statement = _empty_workbook_statement()
        else:
            continue
        statement_id = statement.get("id")
        if not isinstance(statement_id, str) or statement_id in used_statement_ids:
            continue
        allowed_statuses = {
            "documented",
            "inferred",
            "not_documented",
            "conflicting",
            "unresolved",
            "not_applicable",
            "unknown",
        }
        if statement.get("status") not in allowed_statuses:
            source_statuses = {
                facts[fact_id].get("status") for fact_id in statement["fact_ids"]
            }
            statement["status"] = (
                "not_documented"
                if source_statuses & {"planned", "not_documented"}
                else "unknown"
            )
        if (
            statement_type in {"synthesis", "gap"}
            and statement.get("status") == "documented"
            and any(
                facts[fact_id].get("status")
                in {"planned", "not_documented", "unresolved", "unknown"}
                for fact_id in statement["fact_ids"]
            )
        ):
            statement["status"] = "not_documented"
        if (
            statement.get("status") == "documented"
            and any(
                relations[relation_id].get("status") != "documented"
                for relation_id in statement["relation_ids"]
            )
        ):
            statement["status"] = "inferred"
        used_statement_ids.add(statement_id)
        normalized.append(statement)

    if not normalized:
        if facts:
            raise AuditLLMError(
                "audit brief에 유형별 근거 경계를 충족하는 statement가 없습니다."
            )
        normalized = [_empty_workbook_statement()]
    authored["statements"] = normalized
    statement_by_id = {
        item["id"]: item for item in normalized if isinstance(item.get("id"), str)
    }
    summary = authored.get("summary")
    requested_summary_ids = (
        summary.get("statement_ids", []) if isinstance(summary, Mapping) else []
    )
    summary_ids = _known_ids(requested_summary_ids, set(statement_by_id))[:3]
    if not summary_ids:
        summary_ids = list(statement_by_id)[:3]
    authored["summary"] = {
        "text": " ".join(statement_by_id[item]["text"] for item in summary_ids),
        "statement_ids": summary_ids,
    }
    workpaper = authored.get("workpaper")
    if isinstance(workpaper, dict):
        workpaper["fact_ids"] = _known_ids(workpaper.get("fact_ids"), set(facts))

    fact_limitation_ids = {
        item["id"] for item in _records(audit_facts, "limitations")
        if isinstance(item.get("id"), str)
    }
    context_limitation_ids = {
        item["id"] for item in _records(standards_context, "limitations")
        if isinstance(item.get("id"), str)
    }
    limitations: list[dict] = []
    used_limitation_ids: set[str] = set()
    for raw in authored.get("limitations", []):
        if not isinstance(raw, dict):
            continue
        limitation = copy.deepcopy(raw)
        limitation_id = limitation.get("id")
        if not isinstance(limitation_id, str) or limitation_id in used_limitation_ids:
            continue
        raw_fact_limitation_ids = limitation.get("audit_facts_limitation_ids", [])
        raw_context_limitation_ids = limitation.get(
            "standards_context_limitation_ids", []
        )
        if (
            not isinstance(raw_fact_limitation_ids, list)
            or not isinstance(raw_context_limitation_ids, list)
            or any(item not in fact_limitation_ids for item in raw_fact_limitation_ids)
            or any(
                item not in context_limitation_ids
                for item in raw_context_limitation_ids
            )
        ):
            continue
        limitation["audit_facts_limitation_ids"] = list(raw_fact_limitation_ids)
        limitation["standards_context_limitation_ids"] = list(
            raw_context_limitation_ids
        )
        if not (
            limitation["audit_facts_limitation_ids"]
            or limitation["standards_context_limitation_ids"]
        ):
            continue
        limitation["affected_statement_ids"] = _known_ids(
            limitation.get("affected_statement_ids"), set(statement_by_id)
        )
        used_limitation_ids.add(limitation_id)
        limitations.append(limitation)
    authored["limitations"] = limitations


def _normalize_and_validate_authored_records(
    authored: dict,
    audit_facts: Mapping[str, object],
    standards_context: Mapping[str, object],
    strict_schema: dict,
) -> None:
    """Normalize model selections, then enforce the strict authored contract in-call.

    The provider-facing validation schema deliberately relaxes statement source/status rules so
    code can safely drop an entire statement instead of laundering its prose by pruning IDs.  Any
    type-specific constraint that remains invalid after that normalization must still be raised
    inside :func:`call_json`, where the model gets the configured correction retry.
    """
    _normalize_authored_records(authored, audit_facts, standards_context)
    jsonschema.validate(authored, strict_schema)


def _standard_references(text: object) -> set[tuple[str, str]]:
    if not isinstance(text, str):
        return set()
    references = {
        (framework.upper().replace("-", ""), standard_no)
        for framework, standard_no in _STANDARD_REFERENCE_RE.findall(text)
    }
    for framework, pattern in _KOREAN_STANDARD_REFERENCE_PATTERNS:
        references.update((framework, number) for number in pattern.findall(text))
    return references


def _citation_standard_references(citation: Mapping[str, object]) -> set[tuple[str, str]]:
    references: set[tuple[str, str]] = set()
    for value in (
        citation.get("document_id"),
        (
            citation.get("provider_metadata", {}).get("source_cid")
            if isinstance(citation.get("provider_metadata"), Mapping)
            else None
        ),
    ):
        references.update(_standard_references(value))
    provider = citation.get("provider_metadata")
    framework = citation.get("framework")
    if isinstance(provider, Mapping) and isinstance(framework, str):
        standard_no = provider.get("standard_no")
        if isinstance(standard_no, str) and standard_no:
            references.add((framework.upper().replace("-", ""), standard_no))
    return references


def _validate_authored_standard_references(
    authored: dict,
    standards_context: Mapping[str, object],
) -> None:
    """Reject a named standard number unless that statement directly cites the standard."""
    citations = {
        citation["id"]: citation
        for citation in _records(standards_context, "citations")
        if isinstance(citation.get("id"), str)
    }
    for statement in authored.get("statements", []):
        if not isinstance(statement, Mapping):
            continue
        mentioned = _standard_references(statement.get("text"))
        if not mentioned:
            continue
        allowed: set[tuple[str, str]] = set()
        for citation_id in statement.get("standard_citation_ids", []):
            citation = citations.get(citation_id)
            if citation is not None:
                allowed.update(_citation_standard_references(citation))
        unsupported = sorted(mentioned - allowed)
        if unsupported:
            labels = [f"{framework} {number}" for framework, number in unsupported]
            raise AuditLLMError(
                f"statement {statement.get('id')!r}가 직접 인용하지 않은 기준서 번호를 "
                f"언급했습니다: {labels}"
            )


def _drop_unsupported_standard_reference_statements(
    authored: dict,
    standards_context: Mapping[str, object],
) -> list[str]:
    """Fail closed by removing whole statements that name an uncited standard."""
    dropped: list[str] = []
    for statement in authored["statements"]:
        try:
            _validate_authored_standard_references(
                {"statements": [statement]}, standards_context
            )
        except AuditLLMError:
            statement_id = statement.get("id")
            if isinstance(statement_id, str):
                dropped.append(statement_id)
    if not dropped:
        return []
    dropped_ids = set(dropped)
    authored["statements"] = [
        statement
        for statement in authored["statements"]
        if statement.get("id") not in dropped_ids
    ]
    if not authored["statements"]:
        raise AuditLLMError(
            "직접 인용하지 않은 기준서 번호를 제거한 뒤 statement가 남지 않았습니다."
        )
    statement_by_id = {
        statement["id"]: statement for statement in authored["statements"]
    }
    summary_ids = [
        statement_id
        for statement_id in authored["summary"]["statement_ids"]
        if statement_id in statement_by_id
    ]
    if not summary_ids:
        summary_ids = list(statement_by_id)
    authored["summary"] = {
        "text": " ".join(
            statement_by_id[statement_id]["text"] for statement_id in summary_ids[:3]
        ),
        "statement_ids": summary_ids,
    }
    for limitation in authored["limitations"]:
        limitation["affected_statement_ids"] = [
            statement_id
            for statement_id in limitation.get("affected_statement_ids", [])
            if statement_id not in dropped_ids
        ]
    return dropped


def _unresolved_open_item_ids(audit_facts: Mapping[str, object]) -> list[str]:
    return sorted(
        fact["id"]
        for fact in _records(audit_facts, "facts")
        if fact.get("type") == "open_item"
        and fact.get("status") == "unresolved"
        and isinstance(fact.get("id"), str)
    )


def _input_limitation(
    limitation: dict,
    *,
    artifact: str,
    used_ids: set[str],
) -> dict:
    identity = {"artifact": artifact, "id": limitation.get("id")}
    base_id = f"brief-limit:input:{json_sha256(identity)[:16]}"
    limitation_id = base_id
    suffix = 2
    while limitation_id in used_ids:
        limitation_id = f"{base_id}:{suffix}"
        suffix += 1
    used_ids.add(limitation_id)
    facts_ids = [limitation["id"]] if artifact == "audit_facts" else []
    context_ids = [limitation["id"]] if artifact == "standards_context" else []
    return {
        "id": limitation_id,
        "description": (
            f"Unresolved {artifact} input limitation: {limitation['description']}"
        ),
        "severity": limitation["severity"],
        "audit_facts_limitation_ids": facts_ids,
        "standards_context_limitation_ids": context_ids,
        "affected_statement_ids": [],
    }


def _surface_input_limitations(
    authored: dict,
    audit_facts: Mapping[str, object],
    standards_context: Mapping[str, object],
) -> None:
    """Ensure every upstream limitation remains visible and cannot be downplayed."""
    brief_limitations = authored["limitations"]
    used_ids = {
        item.get("id") for item in brief_limitations if isinstance(item.get("id"), str)
    }
    fact_limitations = _records(audit_facts, "limitations")
    context_limitations = _records(standards_context, "limitations")
    fact_by_id = {item["id"]: item for item in fact_limitations}
    context_by_id = {item["id"]: item for item in context_limitations}

    covered_facts: set[str] = set()
    covered_context: set[str] = set()
    for limitation in brief_limitations:
        fact_ids = limitation.get("audit_facts_limitation_ids", [])
        context_ids = limitation.get("standards_context_limitation_ids", [])
        covered_facts.update(item for item in fact_ids if isinstance(item, str))
        covered_context.update(item for item in context_ids if isinstance(item, str))
        source_severities = [
            source["severity"]
            for source_id in fact_ids
            if (source := fact_by_id.get(source_id)) is not None
        ] + [
            source["severity"]
            for source_id in context_ids
            if (source := context_by_id.get(source_id)) is not None
        ]
        if source_severities:
            limitation["severity"] = max(
                [limitation["severity"], *source_severities],
                key=_SEVERITY_ORDER.__getitem__,
            )

    for artifact, limitations, covered in (
        ("audit_facts", fact_limitations, covered_facts),
        ("standards_context", context_limitations, covered_context),
    ):
        for limitation in sorted(limitations, key=lambda item: item["id"]):
            if limitation["id"] not in covered:
                brief_limitations.append(
                    _input_limitation(
                        limitation, artifact=artifact, used_ids=used_ids
                    )
                )


def _readiness_blockers(
    audit_facts: Mapping[str, object],
    standards_context: Mapping[str, object],
    unresolved_open_items: list[str],
) -> tuple[bool, list[str]]:
    facts = _records(audit_facts, "facts")
    fact_limitations = _records(audit_facts, "limitations")
    context_limitations = _records(standards_context, "limitations")
    reasons: list[str] = []
    not_ready = not facts
    if not_ready:
        reasons.append("No workbook facts were extracted.")
    context_queries = _records(standards_context, "queries")
    if any(query.get("status") == "error" for query in context_queries):
        reasons.append("One or more standards queries failed.")
    if any(query.get("status") == "no_results" for query in context_queries):
        reasons.append("One or more standards queries returned no results.")
    if any(
        limitation.get("severity") == "high"
        for limitation in [*fact_limitations, *context_limitations]
    ):
        reasons.append("High-severity input limitations remain unresolved.")
    if any(
        limitation.get("code") == "extraction_incomplete"
        for limitation in fact_limitations
    ):
        reasons.append("Workbook fact extraction is incomplete.")
    if any(
        limitation.get("code") in _READINESS_CONTEXT_LIMITATION_CODES
        for limitation in context_limitations
    ):
        reasons.append("Standards framework or effective date remains unverified.")
    if unresolved_open_items:
        reasons.append("Unresolved open items remain.")
    return not_ready, reasons


def _drop_source_free_model_gaps(authored: dict) -> None:
    """Remove model-authored gaps that have no workbook or standards provenance.

    The schema permits exactly one source-free shape for the deterministic empty-workbook
    readiness record.  When workbook facts exist, the model is never allowed to use that escape
    hatch.  Rebuild the summary from the remaining grounded statements so dropped text cannot
    survive indirectly.
    """
    dropped_ids = {
        statement.get("id")
        for statement in authored["statements"]
        if statement.get("type") == "gap"
        and not statement.get("fact_ids")
        and not statement.get("standard_citation_ids")
    }
    if not dropped_ids:
        return
    authored["statements"] = [
        statement
        for statement in authored["statements"]
        if statement.get("id") not in dropped_ids
    ]
    if not authored["statements"]:
        raise AuditLLMError(
            "audit brief에 provenance가 있는 statement가 하나도 없습니다."
        )
    statement_by_id = {
        statement["id"]: statement for statement in authored["statements"]
    }
    summary_ids = [
        statement_id
        for statement_id in authored["summary"]["statement_ids"]
        if statement_id in statement_by_id
    ]
    if not summary_ids:
        summary_ids = list(statement_by_id)
    authored["summary"] = {
        "text": " ".join(
            statement_by_id[statement_id]["text"] for statement_id in summary_ids[:3]
        ),
        "statement_ids": summary_ids,
    }
    for limitation in authored["limitations"]:
        limitation["affected_statement_ids"] = [
            statement_id
            for statement_id in limitation.get("affected_statement_ids", [])
            if statement_id not in dropped_ids
        ]


def _complete_relation_endpoints(
    authored: dict,
    audit_facts: Mapping[str, object],
) -> None:
    """Make every selected relationship self-contained in its statement evidence list."""
    relations = {
        relation["id"]: relation
        for relation in _records(audit_facts, "relations")
        if isinstance(relation.get("id"), str)
    }
    for statement in authored["statements"]:
        fact_ids = statement["fact_ids"]
        seen = set(fact_ids)
        for relation_id in statement["relation_ids"]:
            relation = relations.get(relation_id)
            if relation is None:
                continue
            for field in ("from_fact_id", "to_fact_id"):
                fact_id = relation.get(field)
                if isinstance(fact_id, str) and fact_id not in seen:
                    fact_ids.append(fact_id)
                    seen.add(fact_id)


def _enforce_integrity(
    authored: dict,
    audit_facts: Mapping[str, object],
    standards_context: Mapping[str, object],
) -> dict:
    """Deterministically retain upstream identity, blockers, gaps, and limitations."""
    result = copy.deepcopy(authored)
    dropped_standard_statements = _drop_unsupported_standard_reference_statements(
        result, standards_context
    )
    source_workpaper = audit_facts.get("workpaper")
    if isinstance(source_workpaper, Mapping):
        for field in _WORKPAPER_FIELDS:
            result["workpaper"][field] = copy.deepcopy(source_workpaper.get(field))

    open_item_ids = _unresolved_open_item_ids(audit_facts)
    result["readiness"]["open_item_fact_ids"] = open_item_ids
    no_facts, blocker_reasons = _readiness_blockers(
        audit_facts, standards_context, open_item_ids
    )
    if no_facts:
        result["readiness"]["status"] = "not_ready"
    elif (
        (blocker_reasons or dropped_standard_statements)
        and result["readiness"]["status"] == "ready"
    ):
        result["readiness"]["status"] = "partial"
    result["readiness"]["reasons"] = list(dict.fromkeys([
        *result["readiness"]["reasons"],
        *blocker_reasons,
        *(
            [
                "Statements naming standards not directly cited by their evidence were "
                "omitted: " + ", ".join(dropped_standard_statements)
            ]
            if dropped_standard_statements else []
        ),
    ]))

    if no_facts:
        statement_id = "statement:empty_workbook"
        result["workpaper"]["fact_ids"] = []
        result["statements"] = [_empty_workbook_statement()]
        result["summary"] = {
            "text": (
                "No workbook facts were extracted; the workpaper is not ready for "
                "substantive audit analysis."
            ),
            "statement_ids": [statement_id],
        }
    else:
        _drop_source_free_model_gaps(result)

    _complete_relation_endpoints(result, audit_facts)
    _surface_input_limitations(result, audit_facts, standards_context)
    return result


def build_audit_brief(
    audit_facts: dict,
    standards_context: dict,
    *,
    client,
    model: str,
    analysis_scope: Mapping[str, object] | None = None,
    generated_at: str | None = None,
    eprint=None,
) -> dict:
    """Synthesize and validate ``data/audit_brief.json``.

    Workbook observations and standards passages remain separate inputs and separate citation ID
    arrays in every output statement.  Cross-artifact ID validity is checked by the bundle
    validator; this function owns structured generation and the artifact's own JSON Schema.
    """
    full_schema = load_schema("audit_brief.schema.json")
    prompt, prompt_sha = load_prompt(BRIEF_PROMPT)
    user = (
        "# audit_facts (workbook-only)\n"
        + json.dumps(audit_facts, ensure_ascii=False, separators=(",", ":"))
        + "\n\n# standards_context (authoritative context only)\n"
        + json.dumps(standards_context, ensure_ascii=False, separators=(",", ":"))
        + "\n\nCreate the model-authored audit brief fields."
    )
    if analysis_scope is not None:
        user += (
            "\n\n# analysis_scope (application-enforced)\n"
            + json.dumps(
                analysis_scope, ensure_ascii=False, separators=(",", ":")
            )
            + "\nOnly the named worksheet was observed. Treat dependency_sheets as formula "
            "reference indicators only: their cell contents were not observed and are not "
            "evidence. Do not make workbook-wide conclusions from this sheet-scoped input."
        )
    authored = call_json(
        client,
        system=prompt,
        user=user,
        schema=(provider_schema := _output_schema(full_schema)),
        validation_schema=_pre_integrity_schema(provider_schema),
        semantic_validator=lambda document: _normalize_and_validate_authored_records(
            document, audit_facts, standards_context, provider_schema
        ),
        label="audit brief",
        retries=1,
        eprint=eprint,
    )
    authored = _enforce_integrity(authored, audit_facts, standards_context)
    generated_at = generated_at or _now_iso()
    brief = {
        "schema_version": "audit_brief.v2",
        "inputs": {
            "audit_facts_sha256": json_sha256(audit_facts),
            "standards_context_sha256": json_sha256(standards_context),
            "workbook_sha256": audit_facts["source"]["sha256"],
        },
        "generator": {
            "name": "excel_to_skill.audit.brief",
            "version": BRIEF_VERSION,
            "model": model,
            "prompt_sha256": prompt_sha,
            "generated_at": generated_at,
        },
        "review": {"status": "draft", "reviewed_at": None, "note": None},
        **authored,
    }
    jsonschema.validate(brief, full_schema)
    return brief
