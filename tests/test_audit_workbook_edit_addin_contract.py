from __future__ import annotations

import json
from pathlib import Path

from excel_to_skill.audit.workbook_edit import (
    create_apply_manifest,
    create_edit_approval,
    create_edit_preview,
    create_edit_proposal,
    create_execution_witness,
    verify_execution_witness,
)


FIXTURE = (
    Path(__file__).parents[1]
    / "office-addin"
    / "tests"
    / "fixtures"
    / "backend-verification.json"
)


def test_office_addin_verification_fixture_matches_python_backend_contract() -> None:
    before = {
        "cell": "A1",
        "authored": {"kind": "blank"},
        "calculated_value": None,
        "calculated_type": "empty",
        "number_format": "General",
        "target_constraints": {
            "merged": False,
            "spill": "none",
            "protected": False,
            "table_member": False,
        },
    }
    actual = {
        **before,
        "authored": {"kind": "value", "value": "승인됨"},
        "calculated_value": "승인됨",
        "calculated_type": "string",
    }
    proposal = create_edit_proposal(
        bundle_id="bundle-a",
        snapshot_id="a" * 64,
        workbook_sha256="b" * 64,
        sheet="C",
        changes=[{"cell": "A1", "kind": "set_value", "value": "승인됨"}],
    )
    preview = create_edit_preview(
        proposal,
        office_session_id="office-session-a",
        office_revision_id="revision-a",
        worksheet_id="worksheet-a",
        before=[before],
    )
    approval = create_edit_approval(
        preview,
        approver_id="user-a",
        expires_at="2099-07-14T00:05:00Z",
    )
    manifest = create_apply_manifest(
        preview,
        approval,
        execution_id="execution-a",
        fencing_token=7,
        challenge_nonce="challenge-a",
    )
    witness = create_execution_witness(
        manifest,
        executor_id="user-a",
        outcome="applied",
        observed_before=[before],
        actual_after=[actual],
    )
    expected = verify_execution_witness(manifest, witness)
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == expected
