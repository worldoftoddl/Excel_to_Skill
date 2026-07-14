from __future__ import annotations

import copy
import hashlib
import json

import jsonschema
import pytest

from excel_to_skill.audit import workbook_edit
from excel_to_skill.audit.model import json_sha256
from excel_to_skill.resources import SCHEMA_DIR
from excel_to_skill.audit.workbook_edit import (
    MAX_ARTIFACT_PAYLOAD_BYTES,
    MAX_CHANGES,
    MAX_FORMULA_REFERENCE_CELLS,
    MAX_PROPOSAL_REFERENCE_CELLS,
    MAX_SAFE_INTEGER,
    WorkbookEditError,
    create_apply_manifest,
    create_edit_approval,
    create_edit_preview,
    create_edit_proposal,
    create_execution_witness,
    verify_execution_witness,
)


SAFE_TARGET = {
    "merged": False,
    "spill": "none",
    "protected": False,
    "table_member": False,
}


def _state(
    cell: str,
    authored: dict,
    calculated_value,
    calculated_type: str,
    *,
    number_format: str = "General",
    constraints: dict | None = None,
) -> dict:
    return {
        "cell": cell,
        "authored": authored,
        "calculated_value": calculated_value,
        "calculated_type": calculated_type,
        "number_format": number_format,
        "target_constraints": copy.deepcopy(constraints or SAFE_TARGET),
    }


def _proposal(changes=None, *, sheet="매출 채권"):
    return create_edit_proposal(
        bundle_id="bundle-a",
        snapshot_id="a" * 64,
        workbook_sha256="b" * 64,
        sheet=sheet,
        changes=changes
        or [
            {"cell": "A1", "kind": "set_value", "value": 2},
            {"cell": "A2", "kind": "set_formula", "formula": "=SUM(A1:A1)"},
            {"cell": "A3", "kind": "set_number_format", "number_format": "#,##0"},
            {"cell": "A4", "kind": "clear_contents"},
        ],
    )


def _before():
    return [
        _state("A1", {"kind": "value", "value": 1}, 1, "number"),
        _state("A2", {"kind": "blank"}, None, "empty"),
        _state("A3", {"kind": "value", "value": 5}, 5, "number"),
        _state(
            "A4",
            {"kind": "formula", "formula": "=SUM(A1:A3)"},
            "#N/A",
            "error",
        ),
    ]


def _preview(proposal=None, before=None):
    return create_edit_preview(
        proposal or _proposal(),
        office_session_id="office-session-1",
        office_revision_id="revision-7",
        worksheet_id="worksheet-abc",
        before=before or _before(),
    )


def _approval(preview=None):
    return create_edit_approval(
        preview or _preview(),
        approver_id="auditor-1",
        expires_at="2026-07-14T18:00:00+09:00",
    )


def _manifest(preview=None, approval=None):
    selected_preview = preview or _preview()
    return create_apply_manifest(
        selected_preview,
        approval or _approval(selected_preview),
        execution_id="execution-1",
        fencing_token=3,
        challenge_nonce="challenge-1",
    )


def _actual_after(preview):
    values = []
    calculated = {
        "A1": (2, "number"),
        "A2": (2, "number"),
        "A3": (5, "number"),
        "A4": (None, "empty"),
    }
    for item in preview["expected_after"]:
        value, kind = calculated[item["cell"]]
        values.append(
            _state(
                item["cell"],
                copy.deepcopy(item["authored"]),
                value,
                kind,
                number_format=item["number_format"],
            )
        )
    return values


def test_proposal_is_canonical_content_addressed_and_exact_sheet_bound():
    changes = [
        {"cell": "B2", "kind": "clear_contents"},
        {"cell": "a1", "kind": "set_value", "value": 10},
    ]
    proposal = _proposal(changes, sheet="매출채권")
    reversed_proposal = _proposal(list(reversed(changes)), sheet="매출채권")

    assert proposal == reversed_proposal
    assert [item["cell"] for item in proposal["changes"]] == ["A1", "B2"]
    assert proposal["binding"]["scope"] == {
        "kind": "sheet",
        "sheet": "매출채권",
        "id": hashlib.sha256("매출채권".encode("utf-8")).hexdigest(),
    }
    assert proposal["proposal_ref"] == "edit-proposal:" + proposal["proposal_sha256"]
    assert proposal["status"] == "proposed"
    assert proposal["review_status"] == "unreviewed"
    assert proposal["application_status"] == "not_applied"
    assert proposal["outside_prepared_bundle"] is True


@pytest.mark.parametrize("cell", ["A1:B2", "A:A", "R1C1", "XFE1", "A1048577"])
def test_proposal_rejects_non_single_or_out_of_grid_cells(cell):
    with pytest.raises(WorkbookEditError) as caught:
        _proposal([{"cell": cell, "kind": "clear_contents"}])
    assert caught.value.code == "INVALID_INPUT"


def test_proposal_rejects_duplicate_cells_and_change_limit():
    with pytest.raises(WorkbookEditError) as duplicate:
        _proposal(
            [
                {"cell": "A1", "kind": "set_value", "value": 1},
                {"cell": "A1", "kind": "set_number_format", "number_format": "0"},
            ]
        )
    assert duplicate.value.code == "DUPLICATE_CELL"

    with pytest.raises(WorkbookEditError) as excessive:
        _proposal(
            [
                {"cell": f"A{index}", "kind": "set_value", "value": index}
                for index in range(1, MAX_CHANGES + 2)
            ]
        )
    assert excessive.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("value", ["=1+1", " +cmd", "-1", "@SUM(A1:A2)"])
def test_set_value_blocks_literal_formula_injection(value):
    with pytest.raises(WorkbookEditError) as caught:
        _proposal([{"cell": "A1", "kind": "set_value", "value": value}])
    assert caught.value.code == "FORMULA_INJECTION_BLOCKED"

    assert _proposal([{"cell": "A1", "kind": "set_value", "value": -1}])
    assert _proposal([{"cell": "A1", "kind": "set_value", "value": "'=1+1"}])


@pytest.mark.parametrize(
    "value",
    [
        9_007_199_254_740_992,
        -9_007_199_254_740_992,
        9_007_199_254_740_992.0,
        -9_007_199_254_740_992.0,
        1e20,
    ],
)
def test_cell_numbers_are_bounded_to_exact_javascript_excel_range(value):
    with pytest.raises(WorkbookEditError) as caught:
        _proposal([{"cell": "A1", "kind": "set_value", "value": value}])
    assert caught.value.code == "INVALID_INPUT"
    assert MAX_SAFE_INTEGER == 9_007_199_254_740_991

    assert _proposal(
        [{"cell": "A1", "kind": "set_value", "value": MAX_SAFE_INTEGER}]
    )


def test_bool_and_number_are_never_equal_in_noop_or_verification_checks():
    proposal = _proposal([{"cell": "A1", "kind": "set_value", "value": 1}])
    before = [_state("A1", {"kind": "value", "value": True}, True, "boolean")]
    preview = _preview(proposal, before)
    assert preview["diff"][0]["before"]["authored"]["value"] is True
    assert preview["diff"][0]["after"]["authored"]["value"] == 1

    manifest = _manifest(preview)
    actual = [_state("A1", {"kind": "value", "value": True}, True, "boolean")]
    witness = create_execution_witness(
        manifest,
        executor_id="excel-addin-1",
        outcome="applied",
        observed_before=before,
        actual_after=actual,
    )
    result = verify_execution_witness(manifest, witness)
    assert result["status"] == "verification_failed"
    assert result["expected_after_matches"] is False

    with pytest.raises(WorkbookEditError) as mismatched_state:
        _preview(
            proposal,
            [_state("A1", {"kind": "value", "value": 1}, True, "number")],
        )
    assert mismatched_state.value.code == "INVALID_INPUT"

    canonical = _proposal([{"cell": "A1", "kind": "set_value", "value": 1.0}])
    assert canonical["changes"][0]["value"] == 1
    assert isinstance(canonical["changes"][0]["value"], int)
    with pytest.raises(WorkbookEditError) as numeric_noop:
        _preview(
            canonical,
            [_state("A1", {"kind": "value", "value": 1}, 1, "number")],
        )
    assert numeric_noop.value.code == "NO_OP_EDIT"


def test_artifact_and_http_schemas_reject_unsafe_json_numbers_and_fences():
    artifact_schemas = [
        "audit_workbook_edit_proposal.schema.json",
        "audit_workbook_edit_preview.schema.json",
        "audit_workbook_edit_apply_manifest.schema.json",
        "audit_workbook_edit_witness.schema.json",
    ]
    http_schemas = [
        "audit_workbook_edit_http_propose_request.schema.json",
        "audit_workbook_edit_http_preview_request.schema.json",
        "audit_workbook_edit_http_verify_request.schema.json",
    ]
    for filename in artifact_schemas:
        schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
        scalar = schema["definitions"]["scalar"]
        jsonschema.validate(MAX_SAFE_INTEGER, scalar)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(MAX_SAFE_INTEGER + 1, scalar)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(float(MAX_SAFE_INTEGER + 1), scalar)
    for filename in http_schemas:
        schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
        scalar = schema["$defs"]["scalar"]
        jsonschema.validate(MAX_SAFE_INTEGER, scalar)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(MAX_SAFE_INTEGER + 1, scalar)

    for filename, location in (
        ("audit_workbook_edit_apply_manifest.schema.json", ("properties", "fencing_token")),
        ("audit_workbook_edit_witness.schema.json", ("properties", "fencing_token")),
        ("audit_workbook_edit_http_execution_request.schema.json", ("properties", "fence")),
        ("audit_workbook_edit_http_verify_request.schema.json", ("properties", "fence")),
    ):
        schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
        fence_schema = schema[location[0]][location[1]]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(MAX_SAFE_INTEGER + 1, fence_schema)


@pytest.mark.parametrize(
    "formula",
    [
        '=INDIRECT("A1")',
        "=OFFSET(A1,1,0)",
        "=Table1[Amount]",
        "='다른 시트'!A1",
        "=[Book.xlsx]Sheet1!A1",
        "=cmd|' /C calc'!A0",
        "=R1C1",
        "=A:A",
        "=A1;A2",
        '=WEBSERVICE("https://example.test")',
        "=A1#",
        "=@SUM(A1:A2)",
        "=NamedRange+1",
        "=SUM(A1:A2) + A3",
    ],
)
def test_set_formula_rejects_dynamic_external_locale_and_unapproved_forms(formula):
    with pytest.raises(WorkbookEditError) as caught:
        _proposal([{"cell": "A1", "kind": "set_formula", "formula": formula}])
    assert caught.value.code == "UNSAFE_FORMULA"


def test_set_formula_accepts_allowlisted_locale_neutral_same_sheet_static_a1():
    proposal = _proposal(
        [
            {
                "cell": "D4",
                "kind": "set_formula",
                "formula": "=SUM($A$1:B2)+IF(C3>0,ROUND(C3,2),0)",
            },
            {
                "cell": "D5",
                "kind": "set_formula",
                "formula": "='매출 채권'!A1+1",
            },
        ]
    )
    assert [item["kind"] for item in proposal["changes"]] == [
        "set_formula",
        "set_formula",
    ]


@pytest.mark.parametrize(
    "formula",
    [
        "=B1:B2",
        "=A1:A2*2",
        "=IF(A1,B1:B2,0)",
        "=INDEX(A1:B2,1,1)",
        "=XLOOKUP(A1,B1:B2,C1:C2)",
    ],
)
def test_set_formula_rejects_multi_cell_dynamic_return_shapes(formula):
    with pytest.raises(WorkbookEditError) as caught:
        _proposal([{"cell": "Z1", "kind": "set_formula", "formula": formula}])
    assert caught.value.code == "UNSAFE_FORMULA"


@pytest.mark.parametrize(
    "formula",
    [
        "=SUM(A1:A100)",
        "=AVERAGE(A1:A100)",
        '=COUNTIF(A1:A100,">0")',
        '=SUMIF(A1:A100,">0",B1:B100)',
        "=MATCH(A1,B1:B100,0)",
        "=VLOOKUP(A1,B1:C100,2,FALSE)",
        "=IF(A1,SUM(B1:B10),AVERAGE(C1:C10))",
    ],
)
def test_set_formula_allows_bounded_ranges_only_inside_scalar_consumers(formula):
    assert _proposal([{"cell": "Z1", "kind": "set_formula", "formula": formula}])


def test_formula_reference_footprint_is_bounded_per_formula_and_proposal():
    accepted = _proposal(
        [{"cell": "Z1", "kind": "set_formula", "formula": "=SUM(A1:A100000)"}]
    )
    assert accepted
    assert MAX_FORMULA_REFERENCE_CELLS == 100_000

    with pytest.raises(WorkbookEditError) as formula_limit:
        _proposal(
            [{"cell": "Z1", "kind": "set_formula", "formula": "=SUM(A1:A100001)"}]
        )
    assert formula_limit.value.code == "LIMIT_EXCEEDED"

    with pytest.raises(WorkbookEditError) as whole_sheet:
        _proposal(
            [{
                "cell": "Z1",
                "kind": "set_formula",
                "formula": "=SUM(A1:XFD1048576)",
            }]
        )
    assert whole_sheet.value.code == "LIMIT_EXCEEDED"

    with pytest.raises(WorkbookEditError) as proposal_limit:
        _proposal(
            [
                {
                    "cell": f"Z{index}",
                    "kind": "set_formula",
                    "formula": "=SUM(A1:A90000)",
                }
                for index in range(1, 4)
            ]
        )
    assert proposal_limit.value.code == "LIMIT_EXCEEDED"
    assert MAX_PROPOSAL_REFERENCE_CELLS == 250_000


def test_proposal_formula_budget_analyzes_once_and_stops_at_first_cumulative_overflow(
    monkeypatch,
):
    original = workbook_edit._analyze_formula
    calls: list[str] = []

    def counted(value, *, sheet):
        calls.append(value)
        return original(value, sheet=sheet)

    monkeypatch.setattr(workbook_edit, "_analyze_formula", counted)
    formula = "=SUM(A1:A100000)"
    changes = [
        {"cell": f"Z{index}", "kind": "set_formula", "formula": formula}
        for index in range(1, 101)
    ]

    with pytest.raises(WorkbookEditError) as caught:
        _proposal(changes)

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert calls == [formula, formula, formula]


def test_proposal_rejects_self_and_multi_cell_formula_cycles():
    with pytest.raises(WorkbookEditError) as self_cycle:
        _proposal([{"cell": "A1", "kind": "set_formula", "formula": "=A1"}])
    assert self_cycle.value.code == "UNSAFE_FORMULA"

    with pytest.raises(WorkbookEditError) as two_cell_cycle:
        _proposal(
            [
                {"cell": "A1", "kind": "set_formula", "formula": "=B1"},
                {"cell": "B1", "kind": "set_formula", "formula": "=A1"},
            ]
        )
    assert two_cell_cycle.value.code == "UNSAFE_FORMULA"

    assert _proposal(
        [
            {"cell": "A1", "kind": "set_formula", "formula": "=B1"},
            {"cell": "B1", "kind": "set_formula", "formula": "=C1"},
        ]
    )


def test_preview_materializes_exact_before_expected_after_and_diff_digests():
    proposal = _proposal()
    before = list(reversed(_before()))
    preview = _preview(proposal, before)

    assert [item["cell"] for item in preview["before"]] == ["A1", "A2", "A3", "A4"]
    assert preview["before"][3]["calculated_type"] == "error"
    assert preview["office_binding"] == {
        "session_id": "office-session-1",
        "revision_id": "revision-7",
        "worksheet_id": "worksheet-abc",
        "sheet": "매출 채권",
    }
    assert preview["expected_after"] == [
        {"cell": "A1", "authored": {"kind": "value", "value": 2}, "number_format": "General"},
        {"cell": "A2", "authored": {"kind": "formula", "formula": "=SUM(A1:A1)"}, "number_format": "General"},
        {"cell": "A3", "authored": {"kind": "value", "value": 5}, "number_format": "#,##0"},
        {"cell": "A4", "authored": {"kind": "blank"}, "number_format": "General"},
    ]
    assert preview["before_sha256"] == json_sha256(preview["before"])
    assert preview["expected_after_sha256"] == json_sha256(preview["expected_after"])
    assert preview["diff_sha256"] == json_sha256(preview["diff"])
    assert preview["preview_ref"] == "edit-preview:" + preview["preview_sha256"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("merged", True),
        ("spill", "parent"),
        ("spill", "child"),
        ("protected", True),
        ("table_member", True),
    ],
)
def test_preview_rejects_unsafe_office_targets(field, value):
    proposal = _proposal([{"cell": "A1", "kind": "set_value", "value": 2}])
    constraints = dict(SAFE_TARGET)
    constraints[field] = value
    before = [_state("A1", {"kind": "value", "value": 1}, 1, "number", constraints=constraints)]
    with pytest.raises(WorkbookEditError) as caught:
        _preview(proposal, before)
    assert caught.value.code == "UNSAFE_TARGET"


def test_preview_rejects_calculated_type_mismatch_and_noop():
    proposal = _proposal([{"cell": "A1", "kind": "set_value", "value": 2}])
    mismatched = [_state("A1", {"kind": "value", "value": 1}, 1, "string")]
    with pytest.raises(WorkbookEditError) as invalid:
        _preview(proposal, mismatched)
    assert invalid.value.code == "INVALID_INPUT"

    fake_error = [
        _state(
            "A1",
            {"kind": "formula", "formula": "=1+1"},
            "ordinary text",
            "error",
        )
    ]
    with pytest.raises(WorkbookEditError) as invalid_error:
        _preview(proposal, fake_error)
    assert invalid_error.value.code == "INVALID_INPUT"

    noop = [_state("A1", {"kind": "value", "value": 2}, 2, "number")]
    with pytest.raises(WorkbookEditError) as unchanged:
        _preview(proposal, noop)
    assert unchanged.value.code == "NO_OP_EDIT"


def test_tampered_proposal_or_preview_cannot_cross_next_boundary():
    proposal = _proposal()
    tampered_proposal = copy.deepcopy(proposal)
    tampered_proposal["changes"][0]["value"] = 999
    with pytest.raises(WorkbookEditError) as proposal_error:
        _preview(tampered_proposal, _before())
    assert proposal_error.value.code in {"CONTRACT_MISMATCH", "INVALID_INPUT"}

    preview = _preview(proposal)
    tampered_preview = copy.deepcopy(preview)
    tampered_preview["before"][0]["calculated_value"] = 999
    with pytest.raises(WorkbookEditError) as preview_error:
        create_edit_approval(
            tampered_preview,
            approver_id="auditor-1",
            expires_at="2026-07-14T18:00:00+09:00",
        )
    assert preview_error.value.code == "INVALID_INPUT"


def test_approval_and_manifest_bind_expiry_single_execution_fence_and_challenge():
    preview = _preview()
    approval = _approval(preview)
    manifest = _manifest(preview, approval)

    assert approval["preview_sha256"] == preview["preview_sha256"]
    assert approval["expires_at"] == "2026-07-14T18:00:00+09:00"
    assert manifest["execution_id"] == "execution-1"
    assert manifest["fencing_token"] == 3
    assert manifest["challenge_nonce"] == "challenge-1"
    assert manifest["approval_expires_at"] == approval["expires_at"]
    assert manifest["before_sha256"] == preview["before_sha256"]
    assert len(manifest["limitations"]) == 3
    assert manifest["manifest_ref"] == "edit-manifest:" + manifest["manifest_sha256"]

    with pytest.raises(WorkbookEditError) as bad_expiry:
        create_edit_approval(preview, approver_id="auditor-1", expires_at="tomorrow")
    assert bad_expiry.value.code == "INVALID_INPUT"
    with pytest.raises(WorkbookEditError) as bad_fence:
        create_apply_manifest(
            preview,
            approval,
            execution_id="execution-1",
            fencing_token=0,
            challenge_nonce="challenge-1",
        )
    assert bad_fence.value.code == "INVALID_INPUT"


def test_applied_witness_verifies_only_session_authored_state_and_requires_snapshot():
    preview = _preview()
    manifest = _manifest(preview)
    actual = _actual_after(preview)
    actual[1]["calculated_value"] = 999
    actual[1]["calculated_type"] = "number"
    witness = create_execution_witness(
        manifest,
        executor_id="excel-addin-1",
        outcome="applied",
        observed_before=_before(),
        actual_after=actual,
        recalculation="recalculate",
    )
    verification = verify_execution_witness(manifest, witness)

    assert witness["execution_id"] == manifest["execution_id"]
    assert witness["fencing_token"] == manifest["fencing_token"]
    assert witness["challenge_nonce"] == manifest["challenge_nonce"]
    assert verification["status"] == "session_verified"
    assert verification["application_status"] == "applied_session_verified"
    assert verification["asset_persisted"] is False
    assert verification["new_snapshot_required"] is True
    assert verification["before_matches"] is True
    assert verification["expected_after_matches"] is True
    assert len(verification["limitations"]) == 3
    assert any("executor" in item and "backend" in item for item in verification["limitations"])


def test_set_formula_applied_witness_requires_recalculation_and_nonempty_nonerror_result():
    preview = _preview()
    manifest = _manifest(preview)
    actual = _actual_after(preview)

    with pytest.raises(WorkbookEditError) as no_recalculation:
        create_execution_witness(
            manifest,
            executor_id="excel-addin-1",
            outcome="applied",
            observed_before=_before(),
            actual_after=actual,
            recalculation="none",
        )
    assert no_recalculation.value.code == "CONTRACT_MISMATCH"

    for calculated_value, calculated_type in ((None, "empty"), ("#REF!", "error")):
        unreadable = _actual_after(preview)
        unreadable[1]["calculated_value"] = calculated_value
        unreadable[1]["calculated_type"] = calculated_type
        witness = create_execution_witness(
            manifest,
            executor_id="excel-addin-1",
            outcome="applied",
            observed_before=_before(),
            actual_after=unreadable,
            recalculation="recalculate",
        )
        result = verify_execution_witness(manifest, witness)
        assert result["status"] == "verification_failed"
        assert result["expected_after_matches"] is False


def test_format_only_edit_of_existing_formula_does_not_require_formula_recalculation():
    proposal = _proposal(
        [{"cell": "A1", "kind": "set_number_format", "number_format": "0.00"}]
    )
    before = [
        _state(
            "A1",
            {"kind": "formula", "formula": "=SUM(B1:B2)"},
            "#N/A",
            "error",
        )
    ]
    preview = _preview(proposal, before)
    manifest = _manifest(preview)
    actual = copy.deepcopy(before)
    actual[0]["number_format"] = "0.00"
    witness = create_execution_witness(
        manifest,
        executor_id="excel-addin-1",
        outcome="applied",
        observed_before=before,
        actual_after=actual,
        recalculation="none",
    )
    assert verify_execution_witness(manifest, witness)["status"] == "session_verified"


def test_stale_precondition_does_not_apply_and_after_mismatch_fails_verification():
    preview = _preview()
    manifest = _manifest(preview)
    stale_before = _before()
    stale_before[0] = _state("A1", {"kind": "value", "value": 8}, 8, "number")
    stale_witness = create_execution_witness(
        manifest,
        executor_id="excel-addin-1",
        outcome="stale_precondition",
        observed_before=stale_before,
        actual_after=None,
        recalculation="none",
    )
    stale = verify_execution_witness(manifest, stale_witness)
    assert stale["status"] == "stale_precondition"
    assert stale["application_status"] == "not_applied"
    assert stale["new_snapshot_required"] is False

    actual = _actual_after(preview)
    actual[0] = _state("A1", {"kind": "value", "value": 77}, 77, "number")
    mismatch_witness = create_execution_witness(
        manifest,
        executor_id="excel-addin-1",
        outcome="applied",
        observed_before=_before(),
        actual_after=actual,
    )
    failed = verify_execution_witness(manifest, mismatch_witness)
    assert failed["status"] == "verification_failed"
    assert failed["application_status"] == "application_failed"
    assert failed["expected_after_matches"] is False
    assert failed["new_snapshot_required"] is True


def test_after_target_becoming_unsafe_cannot_be_session_verified():
    preview = _preview()
    manifest = _manifest(preview)
    actual = _actual_after(preview)
    actual[1]["target_constraints"]["spill"] = "parent"
    witness = create_execution_witness(
        manifest,
        executor_id="excel-addin-1",
        outcome="applied",
        observed_before=_before(),
        actual_after=actual,
    )
    result = verify_execution_witness(manifest, witness)
    assert result["status"] == "verification_failed"


def test_witness_manifest_challenge_tamper_is_rejected():
    preview = _preview()
    manifest = _manifest(preview)
    witness = create_execution_witness(
        manifest,
        executor_id="excel-addin-1",
        outcome="applied",
        observed_before=_before(),
        actual_after=_actual_after(preview),
    )
    tampered = copy.deepcopy(witness)
    tampered["challenge_nonce"] = "another-challenge"
    with pytest.raises(WorkbookEditError) as caught:
        verify_execution_witness(manifest, tampered)
    assert caught.value.code == "CONTRACT_MISMATCH"


def test_proposal_preview_and_witness_payloads_fail_closed_above_600kb():
    huge_changes = [
        {"cell": f"A{index}", "kind": "set_value", "value": "x" * 7_000}
        for index in range(1, 101)
    ]
    with pytest.raises(WorkbookEditError) as proposal_limit:
        _proposal(huge_changes)
    assert proposal_limit.value.code == "LIMIT_EXCEEDED"

    changes = [
        {"cell": f"A{index}", "kind": "set_value", "value": index + 100}
        for index in range(1, 101)
    ]
    proposal = _proposal(changes)
    huge_before = [
        _state(
            f"A{index}",
            {"kind": "formula", "formula": "=" + "1" * 6_999},
            index,
            "number",
        )
        for index in range(1, 101)
    ]
    with pytest.raises(WorkbookEditError) as preview_limit:
        _preview(proposal, huge_before)
    assert preview_limit.value.code == "LIMIT_EXCEEDED"

    small_before = [
        _state(f"A{index}", {"kind": "value", "value": index}, index, "number")
        for index in range(1, 101)
    ]
    preview = _preview(proposal, small_before)
    manifest = _manifest(preview)
    huge_actual = [
        _state(
            f"A{index}",
            {"kind": "formula", "formula": "=" + "1" * 6_999},
            index,
            "number",
        )
        for index in range(1, 101)
    ]
    with pytest.raises(WorkbookEditError) as witness_limit:
        create_execution_witness(
            manifest,
            executor_id="excel-addin-1",
            outcome="applied",
            observed_before=small_before,
            actual_after=huge_actual,
        )
    assert witness_limit.value.code == "LIMIT_EXCEEDED"
    assert MAX_ARTIFACT_PAYLOAD_BYTES == 600_000
