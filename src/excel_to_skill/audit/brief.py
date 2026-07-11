"""Build the agent-ready brief from workbook facts and cached standards context."""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping

import jsonschema

from ..meta import _now_iso
from .llm import AuditLLMError, call_json, load_prompt, load_schema
from .model import json_sha256


BRIEF_VERSION = "0.3.0"
BRIEF_PROMPT = "audit_brief_v1.md"

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


def _output_schema(full_schema: dict) -> dict:
    """Schema for only the model-authored portion of ``audit_brief.json``."""
    names = ("readiness", "workpaper", "summary", "statements", "limitations")
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": list(names),
        "properties": {name: full_schema["properties"][name] for name in names},
        "definitions": full_schema["definitions"],
    }


def _records(doc: Mapping[str, object], key: str) -> list[dict]:
    value = doc.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


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


def _enforce_integrity(
    authored: dict,
    audit_facts: Mapping[str, object],
    standards_context: Mapping[str, object],
) -> dict:
    """Deterministically retain upstream identity, blockers, gaps, and limitations."""
    result = copy.deepcopy(authored)
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
    elif blocker_reasons and result["readiness"]["status"] == "ready":
        result["readiness"]["status"] = "partial"
    result["readiness"]["reasons"] = list(dict.fromkeys([
        *result["readiness"]["reasons"],
        *blocker_reasons,
    ]))

    if no_facts:
        statement_id = "statement:empty_workbook"
        result["workpaper"]["fact_ids"] = []
        result["statements"] = [{
            "id": statement_id,
            "section": "open_items",
            "type": "gap",
            "text": (
                "No workbook facts were extracted; substantive audit analysis is not "
                "ready from the extracted workbook content."
            ),
            "status": "unknown",
            "confidence": 1.0,
            "fact_ids": [],
            "standard_citation_ids": [],
        }]
        result["summary"] = {
            "text": (
                "No workbook facts were extracted; the workpaper is not ready for "
                "substantive audit analysis."
            ),
            "statement_ids": [statement_id],
        }
    else:
        _drop_source_free_model_gaps(result)

    _surface_input_limitations(result, audit_facts, standards_context)
    return result


def build_audit_brief(
    audit_facts: dict,
    standards_context: dict,
    *,
    client,
    model: str,
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
    authored = call_json(
        client,
        system=prompt,
        user=user,
        schema=_output_schema(full_schema),
        label="audit brief",
        retries=1,
        eprint=eprint,
    )
    authored = _enforce_integrity(authored, audit_facts, standards_context)
    generated_at = generated_at or _now_iso()
    brief = {
        "schema_version": "audit_brief.v1",
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
