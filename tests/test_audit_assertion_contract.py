from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from excel_to_skill.audit.validate import (
    _check_audit_facts,
    _check_audit_relation_semantics,
    load_audit_schema,
)
from excel_to_skill.audit.extract import _region_response_schema


ASSERTION_CODES = (
    "accuracy",
    "existence",
    "rights_and_obligations",
    "completeness",
    "occurrence",
    "classification",
    "cutoff",
    "valuation",
    "allocation",
    "understandability",
    "presentation",
    "other",
)


def _fact(*, fact_id: str = "fact:1", fact_type: str, code: object) -> dict:
    return {
        "id": fact_id,
        "type": fact_type,
        "description": "documented workbook fact",
        "status": "documented",
        "normalized_code": code,
        "value": None,
        "unit": None,
        "severity": None,
        "confidence": 0.9,
        "source_ids": ["source:1"],
    }


def _fact_validator() -> Draft7Validator:
    repository_schema = load_audit_schema("audit_facts")
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$ref": "#/definitions/auditFact",
        "definitions": repository_schema["definitions"],
    }
    return Draft7Validator(schema)


@pytest.mark.parametrize("code", ASSERTION_CODES)
def test_assertion_fact_accepts_only_canonical_normalized_codes(code: str) -> None:
    errors = list(
        _fact_validator().iter_errors(_fact(fact_type="assertion", code=code))
    )
    assert errors == []


@pytest.mark.parametrize("code", [None, "", "실재성", "DER", "accuracy_valuation"])
def test_assertion_fact_rejects_missing_or_noncanonical_normalized_code(
    code: object,
) -> None:
    errors = list(
        _fact_validator().iter_errors(_fact(fact_type="assertion", code=code))
    )
    assert errors
    assert any(list(error.absolute_path) == ["normalized_code"] for error in errors)


@pytest.mark.parametrize("code", [None, "workpaper_specific_code"])
def test_nonassertion_fact_keeps_existing_open_normalized_code_contract(
    code: object,
) -> None:
    errors = list(
        _fact_validator().iter_errors(_fact(fact_type="procedure", code=code))
    )
    assert errors == []


def test_region_response_schema_enforces_canonical_assertion_code() -> None:
    schema = _region_response_schema(load_audit_schema("audit_facts"), "region:00001")
    candidate = {
        "region_id": "region:00001",
        "facts": [{
            "local_id": "assertion:1",
            "type": "assertion",
            "description": "경영진 주장: 존재",
            "status": "documented",
            "normalized_code": "실재성",
            "value": None,
            "unit": None,
            "severity": None,
            "confidence": 0.9,
            "sources": [{"ref": "Main!A1", "role": "narrative"}],
        }],
        "limitations": [],
    }

    errors = list(Draft7Validator(schema).iter_errors(candidate))

    assert errors
    assert any(
        list(error.absolute_path) == ["facts", 0, "normalized_code"]
        for error in errors
    )

    candidate["facts"][0]["normalized_code"] = "existence"
    assert list(Draft7Validator(schema).iter_errors(candidate)) == []


def _relation(relation_type: str, from_id: str, to_id: str) -> dict:
    return {
        "id": f"relation:{relation_type}:{from_id}:{to_id}",
        "type": relation_type,
        "from_fact_id": from_id,
        "to_fact_id": to_id,
    }


def _facts_by_id() -> dict[str, dict]:
    fact_types = {
        "fact:procedure": "procedure",
        "fact:assertion": "assertion",
        "fact:risk": "risk",
        "fact:account": "account",
        "fact:control": "control",
        "fact:result": "result",
        "fact:finding": "finding",
        "fact:conclusion": "conclusion",
    }
    return {
        fact_id: {"id": fact_id, "type": fact_type}
        for fact_id, fact_type in fact_types.items()
    }


def test_audit_relation_semantics_accepts_the_four_canonical_directions() -> None:
    relations = [
        _relation("tests", "fact:procedure", "fact:assertion"),
        _relation("tests", "fact:procedure", "fact:control"),
        _relation("addresses", "fact:procedure", "fact:risk"),
        _relation("asserts_over", "fact:assertion", "fact:account"),
        _relation("produces", "fact:procedure", "fact:result"),
        _relation("produces", "fact:procedure", "fact:finding"),
        _relation("relates_to", "fact:risk", "fact:conclusion"),
    ]
    problems: list[str] = []

    _check_audit_relation_semantics(
        _facts_by_id(), list(enumerate(relations)), problems
    )

    assert problems == []


@pytest.mark.parametrize(
    ("relation_type", "from_id", "to_id", "bad_field", "expected_type"),
    [
        ("tests", "fact:risk", "fact:assertion", "from_fact_id", "procedure"),
        ("tests", "fact:procedure", "fact:risk", "to_fact_id", "assertion"),
        ("addresses", "fact:assertion", "fact:risk", "from_fact_id", "procedure"),
        ("addresses", "fact:procedure", "fact:account", "to_fact_id", "risk"),
        ("asserts_over", "fact:procedure", "fact:account", "from_fact_id", "assertion"),
        ("asserts_over", "fact:assertion", "fact:risk", "to_fact_id", "account"),
        ("produces", "fact:assertion", "fact:result", "from_fact_id", "procedure"),
        ("produces", "fact:procedure", "fact:conclusion", "to_fact_id", "finding"),
    ],
)
def test_audit_relation_semantics_rejects_wrong_endpoint_types(
    relation_type: str,
    from_id: str,
    to_id: str,
    bad_field: str,
    expected_type: str,
) -> None:
    problems: list[str] = []

    _check_audit_relation_semantics(
        _facts_by_id(),
        [(3, _relation(relation_type, from_id, to_id))],
        problems,
    )

    assert len(problems) == 1
    assert f"relations[3].{bad_field}" in problems[0]
    assert repr(relation_type) in problems[0]
    assert repr(expected_type) in problems[0]


def test_audit_relation_semantics_leaves_unknown_ids_to_link_validation() -> None:
    problems: list[str] = []

    _check_audit_relation_semantics(
        _facts_by_id(),
        [(0, _relation("tests", "fact:missing", "fact:assertion"))],
        problems,
    )

    assert problems == []


def test_audit_facts_cross_validation_runs_relation_semantics(tmp_path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "data").mkdir(parents=True)
    (pkg / "meta.json").write_text(
        json.dumps({"source": {}, "sheets": []}), encoding="utf-8"
    )
    (pkg / "data" / "cells.jsonl").write_text("", encoding="utf-8")
    facts = {
        "sources": [],
        "facts": [
            {"id": "fact:procedure", "type": "procedure", "source_ids": []},
            {"id": "fact:risk", "type": "risk", "source_ids": []},
        ],
        "relations": [
            _relation("tests", "fact:procedure", "fact:risk"),
        ],
        "standard_queries": [],
        "limitations": [],
    }
    problems: list[str] = []

    _check_audit_facts(pkg, facts, problems)

    assert any(
        "relations[0].to_fact_id" in problem
        and "relation type 'tests'" in problem
        for problem in problems
    )
