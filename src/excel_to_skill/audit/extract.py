"""Workbook-only audit-fact extraction over deterministic regions.

The extraction client is deliberately injected and follows the legacy annotator contract::

    client(system=str, user=str, schema=dict) -> dict | str

This module never imports or constructs an Anthropic client.  Each deterministic region is sent
once, its proposed absolute workbook locators are resolved against ``cells.jsonl``, and only then
are facts bound to source IDs and content digests.  A final consolidation call may connect those
facts, classify the workpaper, and propose standards queries; it cannot add workbook facts.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import jsonschema
from openpyxl.utils import range_boundaries

from ..resources import PROMPT_DIR, SCHEMA_DIR
from .model import AuditModelError, canonical_json, json_sha256
from .llm import AuditLLMError, call_json
from .regions import (
    DEFAULT_MAX_CELLS,
    DEFAULT_MAX_CONTEXT_CELLS,
    DEFAULT_MAX_CONTEXT_ROWS,
    DEFAULT_MAX_ROWS,
    DEFAULT_ROW_GAP,
    AuditRegion,
    build_regions,
)
from .sources import WorkbookSourceResolver
from .validate import AuditValidationError, validate_audit_facts


_SCHEMA_PATH = SCHEMA_DIR / "audit_facts.schema.json"
_REGION_PROMPT_PATH = PROMPT_DIR / "audit_extract_region_v1.md"
_CONSOLIDATE_PROMPT_PATH = PROMPT_DIR / "audit_consolidate_v1.md"

EXTRACTOR_VERSION = "0.2.1"
DEFAULT_OUTPUT = Path("data/audit_facts.json")

ExtractionClient = Callable[..., dict | str]


class AuditExtractionError(AuditModelError):
    """A model response or cross-reference violated the audit-facts extraction contract."""


def _load_json_object(path: Path, *, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise AuditExtractionError(f"{label} 없음: {path}") from e
    except json.JSONDecodeError as e:
        raise AuditExtractionError(f"{label} JSON 파싱 실패: {e}") from e
    if not isinstance(value, dict):
        raise AuditExtractionError(f"{label}은 JSON 객체여야 합니다.")
    return value


def _prompt_bundle() -> tuple[str, str, str]:
    try:
        region = _REGION_PROMPT_PATH.read_text(encoding="utf-8")
        consolidate = _CONSOLIDATE_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise AuditExtractionError(f"audit prompt 없음: {e.filename}") from e
    digest = hashlib.sha256(
        _REGION_PROMPT_PATH.name.encode("utf-8")
        + b"\0"
        + region.encode("utf-8")
        + b"\0"
        + _CONSOLIDATE_PROMPT_PATH.name.encode("utf-8")
        + b"\0"
        + consolidate.encode("utf-8")
    ).hexdigest()
    return region, consolidate, digest


def _call_and_validate(
    client: ExtractionClient,
    *,
    system: str,
    user_payload: dict,
    schema: dict,
    label: str,
) -> dict:
    try:
        doc = call_json(
            client,
            system=system,
            user=canonical_json(user_payload),
            schema=schema,
            label=label,
            retries=1,
        )
    except AuditLLMError as e:
        raise AuditExtractionError(f"{label} 응답 계약 실패: {e}") from e
    # Detach the result from mutable stub/client-owned objects and reject non-JSON values.
    return json.loads(canonical_json(doc))


def _region_response_schema(audit_schema: dict, region_id: str) -> dict:
    definitions = copy.deepcopy(audit_schema["definitions"])
    fact = audit_schema["definitions"]["auditFact"]
    fact_properties = {
        key: copy.deepcopy(value)
        for key, value in fact["properties"].items()
        if key not in {"id", "source_ids"}
    }
    fact_required = [
        key for key in fact["required"] if key not in {"id", "source_ids"}
    ]
    definitions["sourceLocator"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref", "role"],
        "properties": {
            "ref": {"type": "string", "minLength": 1},
            "role": copy.deepcopy(
                audit_schema["definitions"]["workbookSource"]["properties"]["role"]
            ),
        },
    }
    fact_properties.update(
        {
            "local_id": {"$ref": "#/definitions/identifier"},
            "sources": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/definitions/sourceLocator"},
                "uniqueItems": True,
            },
        }
    )
    definitions["regionFact"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["local_id", *fact_required, "sources"],
        "properties": fact_properties,
    }
    # Preserve conditional fact constraints from the repository contract.  In particular,
    # assertion facts must already use a canonical normalized_code at the structured region
    # response boundary; waiting for final-document validation would turn a repairable model
    # response into a late, whole-workbook failure.
    if "allOf" in fact:
        definitions["regionFact"]["allOf"] = copy.deepcopy(fact["allOf"])
    limitation = audit_schema["definitions"]["factLimitation"]
    definitions["regionLimitation"] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "local_id",
            "code",
            "description",
            "severity",
            "affected_local_fact_ids",
            "sources",
        ],
        "properties": {
            "local_id": {"$ref": "#/definitions/identifier"},
            "code": copy.deepcopy(limitation["properties"]["code"]),
            "description": copy.deepcopy(limitation["properties"]["description"]),
            "severity": copy.deepcopy(limitation["properties"]["severity"]),
            "affected_local_fact_ids": {
                "type": "array",
                "items": {"$ref": "#/definitions/identifier"},
                "uniqueItems": True,
            },
            "sources": {
                "type": "array",
                "items": {"$ref": "#/definitions/sourceLocator"},
                "uniqueItems": True,
            },
        },
    }
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": ["region_id", "facts", "limitations"],
        "properties": {
            "region_id": {"const": region_id},
            "facts": {
                "type": "array",
                "items": {"$ref": "#/definitions/regionFact"},
                "uniqueItems": True,
            },
            "limitations": {
                "type": "array",
                "items": {"$ref": "#/definitions/regionLimitation"},
                "uniqueItems": True,
            },
        },
        "definitions": definitions,
    }
    jsonschema.Draft7Validator.check_schema(schema)
    return schema


def _consolidation_response_schema(audit_schema: dict) -> dict:
    definitions = copy.deepcopy(audit_schema["definitions"])
    # Structured-output providers sometimes materialize an omitted optional array as ``[]``.
    # Accept that representation only at the model boundary; the deterministic normalizer below
    # removes it before the authoritative audit_facts schema is applied.
    definitions["standardQuery"]["properties"]["standard_nos"]["minItems"] = 0
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": ["workpaper", "relations", "standard_queries"],
        "properties": {
            "workpaper": {"$ref": "#/definitions/workpaper"},
            "relations": {
                "type": "array",
                "items": {"$ref": "#/definitions/auditRelation"},
                "uniqueItems": True,
            },
            "standard_queries": {
                "type": "array",
                "items": {"$ref": "#/definitions/standardQuery"},
                "uniqueItems": True,
            },
        },
        "definitions": definitions,
    }
    jsonschema.Draft7Validator.check_schema(schema)
    return schema


def _normalize_consolidation_response(document: dict) -> dict:
    """Canonicalize non-substantive provider representations before final validation."""
    for query in document.get("standard_queries", []):
        if isinstance(query, dict) and query.get("standard_nos") == []:
            query.pop("standard_nos")
    return document


def _bounds(ref: str) -> tuple[str, tuple[int, int, int, int]]:
    if not isinstance(ref, str) or "!" not in ref:
        raise AuditExtractionError(f"source ref는 Sheet!A1 절대주소여야 합니다: {ref!r}")
    sheet, coord = ref.rsplit("!", 1)
    try:
        bounds = range_boundaries(coord)
    except (TypeError, ValueError) as e:
        raise AuditExtractionError(f"source ref 주소 파싱 실패: {ref!r}") from e
    if not sheet or any(value is None for value in bounds):
        raise AuditExtractionError(f"source ref 주소가 완전한 셀 범위가 아닙니다: {ref!r}")
    return sheet, bounds


def _source_scope(
    region: AuditRegion,
    ref: str,
    resolver: WorkbookSourceResolver,
) -> str:
    """Return ``region`` or ``context`` for an allowed source locator.

    Context cells may support the interpretation of a current-region observation, but they are
    never allowed to become the only basis for a fact.  Keeping the two scopes separate lets the
    resolver preserve the header/legend provenance without reopening arbitrary cross-region
    citations.
    """
    sheet, (min_col, min_row, max_col, max_row) = _bounds(ref)
    rmin_col, rmin_row, rmax_col, rmax_row = range_boundaries(region.cell_range)
    visible = {(cell["sheet"], cell["row"], cell["col"]) for cell in region.cells}
    resolved = resolver.cells_for(ref)
    if sheet == region.sheet and (
        min_col >= rmin_col
        and min_row >= rmin_row
        and max_col <= rmax_col
        and max_row <= rmax_row
        and all(
            (cell["sheet"], cell["row"], cell["col"]) in visible
            for cell in resolved
        )
    ):
        return "region"

    context_visible = {
        (cell["sheet"], cell["row"], cell["col"])
        for cell in region.context_cells
    }
    if sheet == region.sheet and context_visible:
        context_rows = [cell["row"] for cell in region.context_cells]
        context_cols = [cell["col"] for cell in region.context_cells]
        inside_context_bounds = (
            min_col >= min(context_cols)
            and min_row >= min(context_rows)
            and max_col <= max(context_cols)
            and max_row <= max(context_rows)
        )
        if inside_context_bounds and all(
            (cell["sheet"], cell["row"], cell["col"]) in context_visible
            for cell in resolved
        ):
            return "context"

    raise AuditExtractionError(
        f"{region.region_id}이 관찰 범위 밖 source를 주장했습니다: {ref} "
        f"(현재 {region.sheet}!{region.cell_range}; read-only context는 label 보조근거만 허용)"
    )


def _ensure_inside_region(region: AuditRegion, ref: str, resolver: WorkbookSourceResolver) -> None:
    """Compatibility wrapper for callers that require a primary current-region source."""
    if _source_scope(region, ref, resolver) != "region":
        raise AuditExtractionError(
            f"{region.region_id}의 1차 source는 현재 region 안이어야 합니다: {ref}"
        )


def _resolve_locators(
    region: AuditRegion,
    locators: list[dict],
    resolver: WorkbookSourceResolver,
    sources_by_id: dict[str, dict],
) -> list[str]:
    source_ids: list[str] = []
    has_current_region_source = False
    for locator in locators:
        ref = locator["ref"]
        scope = _source_scope(region, ref, resolver)
        if scope == "context" and locator["role"] != "label":
            raise AuditExtractionError(
                f"{region.region_id} read-only context source는 role='label'만 허용: {ref}"
            )
        has_current_region_source = has_current_region_source or scope == "region"
        source = resolver.resolve(ref).to_dict(role=locator["role"])
        prior = sources_by_id.get(source["id"])
        if prior is not None and prior != source:
            raise AuditExtractionError(f"source ID 충돌: {source['id']}")
        sources_by_id[source["id"]] = source
        if source["id"] not in source_ids:
            source_ids.append(source["id"])
    if not has_current_region_source:
        raise AuditExtractionError(
            f"{region.region_id} fact/limitation source에는 현재 region의 1차 근거가 필요합니다."
        )
    return source_ids


def _materialize_region(
    region: AuditRegion,
    response: dict,
    resolver: WorkbookSourceResolver,
    sources_by_id: dict[str, dict],
) -> tuple[list[dict], list[dict], dict]:
    facts: list[dict] = []
    limitations: list[dict] = []
    local_to_fact: dict[str, str] = {}

    for ordinal, candidate in enumerate(response["facts"], 1):
        local_id = candidate["local_id"]
        if local_id in local_to_fact:
            raise AuditExtractionError(
                f"{region.region_id} fact local_id 중복: {local_id!r}"
            )
        source_ids = _resolve_locators(
            region, candidate["sources"], resolver, sources_by_id
        )
        identity = {
            "region_id": region.region_id,
            "ordinal": ordinal,
            "local_id": local_id,
            "type": candidate["type"],
            "description": candidate["description"],
            "source_ids": source_ids,
        }
        fact_id = "fact:" + json_sha256(identity)[:20]
        local_to_fact[local_id] = fact_id
        facts.append(
            {
                "id": fact_id,
                **{
                    key: copy.deepcopy(value)
                    for key, value in candidate.items()
                    if key not in {"local_id", "sources"}
                },
                "source_ids": source_ids,
            }
        )

    seen_limitation_ids: set[str] = set()
    for ordinal, candidate in enumerate(response["limitations"], 1):
        local_id = candidate["local_id"]
        affected: list[str] = []
        for fact_local_id in candidate["affected_local_fact_ids"]:
            if fact_local_id not in local_to_fact:
                raise AuditExtractionError(
                    f"{region.region_id} limitation이 없는 local fact를 참조: "
                    f"{fact_local_id!r}"
                )
            affected.append(local_to_fact[fact_local_id])
        source_ids = _resolve_locators(
            region, candidate["sources"], resolver, sources_by_id
        )
        identity = {
            "region_id": region.region_id,
            "ordinal": ordinal,
            "local_id": local_id,
            "description": candidate["description"],
        }
        limitation_id = "limitation:" + json_sha256(identity)[:20]
        if limitation_id in seen_limitation_ids:
            raise AuditExtractionError(
                f"{region.region_id} limitation identity 중복: {local_id!r}"
            )
        seen_limitation_ids.add(limitation_id)
        limitations.append(
            {
                "id": limitation_id,
                "code": candidate["code"],
                "description": candidate["description"],
                "severity": candidate["severity"],
                "affected_fact_ids": affected,
                "source_ids": source_ids,
            }
        )

    if not facts and not limitations:
        # A schema-valid empty response must not make an observed workbook region disappear from
        # the preparation record. Bind a deterministic coverage limitation to the whole region.
        source_ids = _resolve_locators(
            region,
            [{"ref": f"{region.sheet}!{region.cell_range}", "role": "mixed"}],
            resolver,
            sources_by_id,
        )
        limitations.append({
            "id": "limitation:" + json_sha256({
                "region_id": region.region_id,
                "code": "extraction_incomplete",
            })[:20],
            "code": "extraction_incomplete",
            "description": "관찰된 region에서 감사 사실 또는 구체적 한계를 추출하지 못했습니다.",
            "severity": "moderate",
            "affected_fact_ids": [],
            "source_ids": source_ids,
        })

    summary = {
        "region_id": region.region_id,
        "sheet": region.sheet,
        "range": region.cell_range,
        "fact_ids": [fact["id"] for fact in facts],
        "limitation_ids": [item["id"] for item in limitations],
    }
    return facts, limitations, summary


def _check_unique_ids(items: list[dict], *, label: str) -> set[str]:
    ids = [item.get("id") for item in items]
    if len(ids) != len(set(ids)):
        raise AuditExtractionError(f"{label} id가 중복되었습니다.")
    return set(ids)


def _check_cross_references(document: dict) -> None:
    source_ids = _check_unique_ids(document["sources"], label="sources")
    fact_ids = _check_unique_ids(document["facts"], label="facts")
    _check_unique_ids(document["relations"], label="relations")
    _check_unique_ids(document["standard_queries"], label="standard_queries")
    _check_unique_ids(document["limitations"], label="limitations")

    def require_ids(values: list[str], allowed: set[str], *, label: str) -> None:
        missing = sorted(set(values) - allowed)
        if missing:
            raise AuditExtractionError(f"{label}이 없는 ID를 참조합니다: {missing}")

    require_ids(document["workpaper"]["source_ids"], source_ids, label="workpaper.source_ids")
    for fact in document["facts"]:
        require_ids(fact["source_ids"], source_ids, label=f"fact {fact['id']}.source_ids")
    for relation in document["relations"]:
        require_ids(
            [relation["from_fact_id"], relation["to_fact_id"]],
            fact_ids,
            label=f"relation {relation['id']} fact refs",
        )
        require_ids(
            relation["source_ids"], source_ids, label=f"relation {relation['id']}.source_ids"
        )
    for query in document["standard_queries"]:
        require_ids(query["fact_ids"], fact_ids, label=f"query {query['id']}.fact_ids")
    for limitation in document["limitations"]:
        require_ids(
            limitation["affected_fact_ids"],
            fact_ids,
            label=f"limitation {limitation['id']}.affected_fact_ids",
        )
        require_ids(
            limitation["source_ids"],
            source_ids,
            label=f"limitation {limitation['id']}.source_ids",
        )


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def extract_audit_facts(
    pkg: Path | str,
    *,
    client: ExtractionClient | None,
    model: str | None = None,
    generated_at: str | None = None,
    output: Path | str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_cells: int = DEFAULT_MAX_CELLS,
    row_gap: int = DEFAULT_ROW_GAP,
    max_context_rows: int = DEFAULT_MAX_CONTEXT_ROWS,
    max_context_cells: int = DEFAULT_MAX_CONTEXT_CELLS,
) -> dict:
    """Create and persist ``audit_facts.json`` using only workbook-region observations.

    There is deliberately no default network client: callers must inject one.  All regions are
    invoked in deterministic order, followed by exactly one consolidation invocation.  The file
    is written only after response schemas, ledger bindings, cross-references, and the complete
    ``audit_facts.schema.json`` contract have passed.
    """
    if client is None:
        raise AuditExtractionError(
            "audit facts 추출 client를 주입해야 합니다(실 Anthropic 자동 호출 없음)."
        )
    pkg = Path(pkg)
    audit_schema = _load_json_object(_SCHEMA_PATH, label="audit_facts schema")
    jsonschema.Draft7Validator.check_schema(audit_schema)
    meta = _load_json_object(pkg / "meta.json", label="meta.json")
    resolver = WorkbookSourceResolver(pkg)
    region_prompt, consolidate_prompt, prompt_sha = _prompt_bundle()
    regions = build_regions(
        pkg,
        max_rows=max_rows,
        max_cells=max_cells,
        row_gap=row_gap,
        max_context_rows=max_context_rows,
        max_context_cells=max_context_cells,
    )

    sources_by_id: dict[str, dict] = {}
    facts: list[dict] = []
    limitations: list[dict] = []
    region_summaries: list[dict] = []
    for region in regions:
        response = _call_and_validate(
            client,
            system=region_prompt,
            user_payload=region.prompt_payload(),
            schema=_region_response_schema(audit_schema, region.region_id),
            label=region.region_id,
        )
        region_facts, region_limitations, summary = _materialize_region(
            region, response, resolver, sources_by_id
        )
        facts.extend(region_facts)
        limitations.extend(region_limitations)
        region_summaries.append(summary)

    source_meta = meta.get("source", {})
    source = {
        "filename": source_meta.get("filename"),
        "sha256": source_meta.get("sha256"),
        "format": source_meta.get("format"),
    }
    sources = sorted(sources_by_id.values(), key=lambda item: item["id"])
    consolidation = _call_and_validate(
        client,
        system=consolidate_prompt,
        user_payload={
            "source": source,
            "sheets": [
                {"name": sheet.get("name"), "dimensions": sheet.get("dimensions")}
                for sheet in meta.get("sheets", [])
                if isinstance(sheet, dict)
            ],
            "regions": region_summaries,
            "facts": facts,
            "sources": sources,
            "limitations": limitations,
        },
        schema=_consolidation_response_schema(audit_schema),
        label="workbook consolidation",
    )
    consolidation = _normalize_consolidation_response(consolidation)

    document = {
        "schema_version": "audit_facts.v1",
        "source": source,
        "generator": {
            "name": "excel_to_skill.audit.extract",
            "version": EXTRACTOR_VERSION,
            "kind": "hybrid",
            "model": model,
            "prompt_sha256": prompt_sha,
            "generated_at": generated_at or _now_iso(),
        },
        "review": {"status": "draft", "reviewed_at": None, "note": None},
        "workpaper": consolidation["workpaper"],
        "sources": sources,
        "facts": facts,
        "relations": consolidation["relations"],
        "standard_queries": consolidation["standard_queries"],
        "limitations": limitations,
    }
    _check_cross_references(document)
    try:
        jsonschema.validate(document, audit_schema)
    except jsonschema.ValidationError as e:
        path = ".".join(str(part) for part in e.absolute_path) or "$"
        raise AuditExtractionError(f"최종 audit_facts schema 실패({path}): {e.message}") from e
    try:
        validate_audit_facts(pkg, document)
    except AuditValidationError as e:
        detail = "; ".join(e.problems[:5])
        suffix = " ..." if len(e.problems) > 5 else ""
        raise AuditExtractionError(
            f"최종 audit_facts 의미·근거 검증 실패({len(e.problems)}건): "
            f"{detail}{suffix}"
        ) from e

    target = Path(output) if output is not None else pkg / DEFAULT_OUTPUT
    if not target.is_absolute() and output is not None:
        target = pkg / target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return document


__all__ = [
    "AuditExtractionError",
    "DEFAULT_OUTPUT",
    "EXTRACTOR_VERSION",
    "extract_audit_facts",
]
