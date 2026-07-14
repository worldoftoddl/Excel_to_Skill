from __future__ import annotations

import asyncio
import builtins
import json
import jsonschema
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import excel_to_skill.audit.workbook_edit_web as edit_web_module
from excel_to_skill.audit.service import ServicePrincipal
from excel_to_skill.audit.workbook_edit_service import (
    InMemoryWorkbookEditRepository,
    InMemoryWorkbookSessionRepository,
    WorkbookEditService,
    WorkbookEditServiceError,
    WorkbookSessionBinding,
)
from excel_to_skill.audit.workbook_edit_web import (
    APPROVE_REQUEST_SCHEMA,
    CLAIM_REQUEST_SCHEMA,
    EXECUTION_REQUEST_SCHEMA,
    PREVIEW_REQUEST_SCHEMA,
    PROPOSE_REQUEST_SCHEMA,
    RECEIPT_SCHEMA,
    REJECT_REQUEST_SCHEMA,
    REQUEST_SCHEMAS,
    RESPONSE_SCHEMAS,
    SUBMISSION_RESPONSE_SCHEMA,
    VERIFY_REQUEST_SCHEMA,
    WORKFLOW_RESPONSE_SCHEMA,
    AuditWorkbookEditHttpAdapter,
    WorkbookEditWebAdapterUnavailableError,
    create_workbook_edit_fastapi_app,
)
from excel_to_skill.audit.workbook_edit import (
    create_apply_manifest,
    create_edit_approval,
    create_edit_preview,
    create_edit_proposal,
    create_execution_witness,
    verify_execution_witness,
)


PRINCIPAL = ServicePrincipal("tenant-a", "user-a")
STATE_BLANK = {
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
STATE_VALUE = {
    **STATE_BLANK,
    "authored": {"kind": "value", "value": "승인됨"},
    "calculated_value": "승인됨",
    "calculated_type": "string",
}
PROPOSE_BODY = {
    "session_id": "office-session-a",
    "changes": [{"cell": "A1", "kind": "set_value", "value": "승인됨"}],
}
PREVIEW_BODY = {
    "office_revision_id": "revision-a",
    "worksheet_id": "worksheet-a",
    "before": [STATE_BLANK],
}
APPROVE_BODY = {"preview_sha256": "a" * 64, "confirmed": True}
CLAIM_BODY = {"session_id": "office-session-a"}
EXECUTION_BODY = {"fence": 1, "challenge": "challenge-a"}
VERIFY_BODY = {
    **EXECUTION_BODY,
    "witness": {
        "outcome": "applied",
        "observed_before": [STATE_BLANK],
        "actual_after": [STATE_VALUE],
        "recalculation": "recalculate",
    },
}

_FAKE_PROPOSAL = create_edit_proposal(
    bundle_id="bundle-a",
    snapshot_id="1" * 64,
    workbook_sha256="2" * 64,
    sheet="C",
    changes=PROPOSE_BODY["changes"],
)
_FAKE_PREVIEW = create_edit_preview(
    _FAKE_PROPOSAL,
    office_session_id="office-session-a",
    office_revision_id="revision-a",
    worksheet_id="worksheet-a",
    before=[STATE_BLANK],
)
_FAKE_APPROVAL = create_edit_approval(
    _FAKE_PREVIEW,
    approver_id=PRINCIPAL.subject_id,
    expires_at="2026-07-14T00:05:00Z",
)
_FAKE_MANIFEST = create_apply_manifest(
    _FAKE_PREVIEW,
    _FAKE_APPROVAL,
    execution_id="execution-a",
    fencing_token=1,
    challenge_nonce="challenge-a",
)
_FAKE_WITNESS = create_execution_witness(
    _FAKE_MANIFEST,
    executor_id=PRINCIPAL.subject_id,
    outcome="applied",
    observed_before=[STATE_BLANK],
    actual_after=[STATE_VALUE],
)
_FAKE_VERIFICATION = verify_execution_witness(_FAKE_MANIFEST, _FAKE_WITNESS)
_FAKE_EXECUTION_DEADLINE = "2026-07-14T00:10:00Z"


class _Receipt:
    def __init__(self, workflow_id: str, phase: str) -> None:
        self.workflow_id = workflow_id
        self.phase = phase

    def to_dict(self):
        state, details = {
            "propose": (
                "proposed",
                {
                    "proposal_ref": _FAKE_PROPOSAL["proposal_ref"],
                    "proposal_sha256": _FAKE_PROPOSAL["proposal_sha256"],
                },
            ),
            "preview": ("previewed", {"preview": _FAKE_PREVIEW}),
            "approve": (
                "approved",
                {
                    "approval_ref": _FAKE_APPROVAL["approval_ref"],
                    "approval_sha256": _FAKE_APPROVAL["approval_sha256"],
                    "expires_at": _FAKE_APPROVAL["expires_at"],
                },
            ),
            "reject": (
                "rejected",
                {
                    "preview_ref": _FAKE_PREVIEW["preview_ref"],
                    "reason": "human_rejected",
                },
            ),
            "claim_execution": (
                "claimed",
                {
                    "execution_id": "execution-a",
                    "fence": 1,
                    "challenge": "challenge-a",
                    "apply_manifest": _FAKE_MANIFEST,
                },
            ),
            "mark_apply_started": (
                "apply_started",
                {
                    "execution_id": "execution-a",
                    "fence": 1,
                    "manifest_ref": _FAKE_MANIFEST["manifest_ref"],
                    "write_started": True,
                    "execution_deadline": _FAKE_EXECUTION_DEADLINE,
                },
            ),
            "verify_execution": (
                "session_verified",
                {"verification": _FAKE_VERIFICATION},
            ),
            "abort_claim": (
                "aborted_before_apply",
                {
                    "execution_id": "execution-a",
                    "fence": 1,
                    "reason": "claim_aborted",
                },
            ),
        }[self.phase]
        return {
            "schema_version": "audit_workbook_edit_receipt.v1",
            "command_id": "command-" + self.phase,
            "workflow_id": self.workflow_id,
            "session_id": "office-session-a",
            "bundle_id": "bundle-a",
            "snapshot_id": "1" * 64,
            "workbook_sha256": "2" * 64,
            "revision_id": "revision-a",
            "sheet": "C",
            "state": state,
            "details": details,
        }


class _RecordingService(WorkbookEditService):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.seen: set[tuple[str, str]] = set()
        self.failure: Exception | None = None

    def _result(self, method: str, kwargs: dict[str, object]):
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        self.calls.append((method, kwargs))
        key = (method, str(kwargs["idempotency_key"]))
        replayed = key in self.seen
        self.seen.add(key)
        return SimpleNamespace(
            receipt=_Receipt(str(kwargs.get("workflow_id", "workflow-a")), method),
            replayed=replayed,
        )

    def propose(self, **kwargs):
        return self._result("propose", kwargs)

    def preview(self, **kwargs):
        return self._result("preview", kwargs)

    def approve(self, **kwargs):
        return self._result("approve", kwargs)

    def reject(self, **kwargs):
        return self._result("reject", kwargs)

    def claim_execution(self, **kwargs):
        return self._result("claim_execution", kwargs)

    def mark_apply_started(self, **kwargs):
        return self._result("mark_apply_started", kwargs)

    def verify_execution(self, **kwargs):
        return self._result("verify_execution", kwargs)

    def abort_claim(self, **kwargs):
        return self._result("abort_claim", kwargs)

    def get_workflow(self, **kwargs):
        self.calls.append(("get_workflow", kwargs))
        return {
            "schema_version": "audit_workbook_edit_workflow.v1",
            "workflow_id": str(kwargs["workflow_id"]),
            "session_id": "office-session-a",
            "bundle_id": "bundle-a",
            "snapshot_id": "1" * 64,
            "workbook_sha256": "2" * 64,
            "revision_id": "revision-a",
            "sheet": "C",
            "worksheet_id": "worksheet-a",
            "state": "proposed",
            "version": 1,
            "approval_consumed": False,
            "execution_id": None,
            "fence": None,
            "execution_deadline": None,
            "refs": {
                "proposal_ref": _FAKE_PROPOSAL["proposal_ref"],
                "preview_ref": None,
                "approval_ref": None,
                "manifest_ref": None,
                "verification_ref": None,
            },
            "artifacts": {
                "proposal": _FAKE_PROPOSAL,
                "preview": None,
                "approval": None,
                "manifest": None,
                "verification": None,
            },
        }


@pytest.fixture
def edit_web():
    service = _RecordingService()
    return service, AuditWorkbookEditHttpAdapter(service)


def test_request_schemas_are_strict_and_match_canonical_public_documents() -> None:
    for schema in REQUEST_SCHEMAS.values():
        jsonschema.Draft202012Validator.check_schema(schema)

    valid = [
        (PROPOSE_BODY, PROPOSE_REQUEST_SCHEMA),
        (PREVIEW_BODY, PREVIEW_REQUEST_SCHEMA),
        (APPROVE_BODY, APPROVE_REQUEST_SCHEMA),
        ({}, REJECT_REQUEST_SCHEMA),
        (CLAIM_BODY, CLAIM_REQUEST_SCHEMA),
        (EXECUTION_BODY, EXECUTION_REQUEST_SCHEMA),
        (VERIFY_BODY, VERIFY_REQUEST_SCHEMA),
    ]
    for document, schema in valid:
        jsonschema.validate(document, schema)

    invalid_proposals = [
        {**PROPOSE_BODY, "path": "/private/client.xlsx"},
        {**PROPOSE_BODY, "provider": "raw"},
        {**PROPOSE_BODY, "token": "opaque-value"},
        {**PROPOSE_BODY, "javascript": "Excel.run(...)"},
        {
            **PROPOSE_BODY,
            "changes": [{"cell": "A1:B2", "kind": "clear_contents"}],
        },
        {**PROPOSE_BODY, "changes": [{"cell": "A1", "kind": "clear"}]},
        {
            **PROPOSE_BODY,
            "changes": [
                {
                    "cell": "A1",
                    "kind": "set_formula",
                    "formula": "SUM(A1:A2)",
                }
            ],
        },
        {
            **PROPOSE_BODY,
            "changes": [{"cell": "A1", "kind": "set_value", "value": "  =1+1"}],
        },
    ]
    for document in invalid_proposals:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, PROPOSE_REQUEST_SCHEMA)


def test_response_schemas_are_closed_and_runtime_rejects_an_invalid_receipt(edit_web) -> None:
    service, adapter = edit_web
    for schema in RESPONSE_SCHEMAS.values():
        jsonschema.validators.validator_for(schema).check_schema(schema)

    valid = _Receipt("workflow-a", "propose").to_dict()
    assert valid["state"] == "proposed"
    assert RECEIPT_SCHEMA["additionalProperties"] is False
    assert SUBMISSION_RESPONSE_SCHEMA["additionalProperties"] is False
    assert WORKFLOW_RESPONSE_SCHEMA["additionalProperties"] is False

    class InvalidReceipt:
        def to_dict(self):
            return {
                **valid,
                "state": "claimed",
                "details": {"challenge": "/private/raw-secret"},
            }

    service.propose = lambda **kwargs: SimpleNamespace(
        receipt=InvalidReceipt(),
        replayed=False,
    )
    response = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "invalid-output"},
        json_body=PROPOSE_BODY,
    )
    assert response.status_code == 500
    assert response.body["error"]["code"] == "INTERNAL_ERROR"
    assert "raw-secret" not in str(response.body)


def test_runtime_response_byte_cap_fails_closed_with_fixed_413(edit_web, monkeypatch) -> None:
    _, adapter = edit_web
    monkeypatch.setattr(edit_web_module, "_MAX_HTTP_RESPONSE_BYTES", 500)
    response = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "bounded-output"},
        json_body=PROPOSE_BODY,
    )
    assert response.status_code == 413
    assert response.body["error"]["code"] == "LIMIT_EXCEEDED"


def test_adapter_proposes_with_no_store_and_stably_reports_service_replay(edit_web) -> None:
    service, adapter = edit_web

    created = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "proposal-key"},
        json_body=PROPOSE_BODY,
    )
    replay = adapter.propose(
        principal=PRINCIPAL,
        headers={"idempotency-key": "proposal-key"},
        json_body=PROPOSE_BODY,
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.headers["Cache-Control"] == "no-store, private"
    assert created.headers["Idempotency-Replayed"] == "false"
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert created.headers["Location"].endswith("/workflow-a")
    assert service.calls[0] == (
        "propose",
        {
            "principal": PRINCIPAL,
            "session_id": "office-session-a",
            "proposal_input": {"changes": PROPOSE_BODY["changes"]},
            "idempotency_key": "proposal-key",
        },
    )


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("propose", {"json_body": PROPOSE_BODY}),
        ("preview", {"workflow_id": "workflow-a", "json_body": PREVIEW_BODY}),
        (
            "approve",
            {
                "workflow_id": "workflow-a",
                "preview_id": "preview-a",
                "json_body": APPROVE_BODY,
            },
        ),
        (
            "reject",
            {
                "workflow_id": "workflow-a",
                "preview_id": "preview-a",
                "json_body": {},
            },
        ),
        (
            "claim_execution",
            {"workflow_id": "workflow-a", "json_body": CLAIM_BODY},
        ),
        (
            "mark_apply_started",
            {
                "workflow_id": "workflow-a",
                "execution_id": "execution-a",
                "json_body": EXECUTION_BODY,
            },
        ),
        (
            "verify_execution",
            {
                "workflow_id": "workflow-a",
                "execution_id": "execution-a",
                "json_body": VERIFY_BODY,
            },
        ),
        (
            "abort_claim",
            {
                "workflow_id": "workflow-a",
                "execution_id": "execution-a",
                "json_body": EXECUTION_BODY,
            },
        ),
    ],
)
def test_every_mutation_requires_exactly_one_idempotency_header(
    edit_web,
    method,
    kwargs,
) -> None:
    service, adapter = edit_web
    call = getattr(adapter, method)

    missing = call(principal=PRINCIPAL, headers={}, **kwargs)
    duplicate = call(
        principal=PRINCIPAL,
        headers=[("Idempotency-Key", "one"), ("idempotency-key", "two")],
        **kwargs,
    )

    assert missing.status_code == duplicate.status_code == 400
    assert missing.body["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"
    assert duplicate.body["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"
    assert service.calls == []


def test_adapter_forwards_exact_preview_approval_execution_and_witness_inputs(edit_web) -> None:
    service, adapter = edit_web
    headers = {"Idempotency-Key": "step-key"}

    calls = [
        lambda: adapter.preview(
            principal=PRINCIPAL,
            workflow_id="workflow-a",
            headers=headers,
            json_body=PREVIEW_BODY,
        ),
        lambda: adapter.approve(
            principal=PRINCIPAL,
            workflow_id="workflow-a",
            preview_id="preview-a",
            headers=headers,
            json_body=APPROVE_BODY,
        ),
        lambda: adapter.reject(
            principal=PRINCIPAL,
            workflow_id="workflow-a",
            preview_id="preview-a",
            headers=headers,
            json_body={},
        ),
        lambda: adapter.claim_execution(
            principal=PRINCIPAL,
            workflow_id="workflow-a",
            headers=headers,
            json_body=CLAIM_BODY,
        ),
        lambda: adapter.mark_apply_started(
            principal=PRINCIPAL,
            workflow_id="workflow-a",
            execution_id="execution-a",
            headers=headers,
            json_body=EXECUTION_BODY,
        ),
        lambda: adapter.verify_execution(
            principal=PRINCIPAL,
            workflow_id="workflow-a",
            execution_id="execution-a",
            headers=headers,
            json_body=VERIFY_BODY,
        ),
        lambda: adapter.abort_claim(
            principal=PRINCIPAL,
            workflow_id="workflow-a",
            execution_id="execution-a",
            headers=headers,
            json_body=EXECUTION_BODY,
        ),
    ]
    for index, call in enumerate(calls):
        response = call()
        assert response.status_code in {200, 201}
        assert response.headers["Cache-Control"] == "no-store, private"
        headers = {"Idempotency-Key": f"step-key-{index}"}

    recorded = dict(service.calls)
    assert recorded["preview"]["preview_input"] == PREVIEW_BODY
    assert recorded["approve"]["preview_id"] == "preview-a"
    assert recorded["approve"]["preview_sha256"] == "a" * 64
    assert recorded["approve"]["confirmed"] is True
    assert recorded["mark_apply_started"]["fence"] == 1
    assert recorded["mark_apply_started"]["challenge"] == "challenge-a"
    assert recorded["verify_execution"]["witness"] == VERIFY_BODY["witness"]

    fetched = adapter.get_workflow(principal=PRINCIPAL, workflow_id="workflow-a")
    assert fetched.status_code == 200
    assert fetched.headers["Cache-Control"] == "no-store, private"
    assert fetched.body["workflow"]["workflow_id"] == "workflow-a"


@pytest.mark.parametrize(
    "body",
    [
        {**PROPOSE_BODY, "package_path": "/tmp/private"},
        {**PROPOSE_BODY, "workbook_sha256": "a" * 64},
        {**PROPOSE_BODY, "provider": "local"},
        {**PROPOSE_BODY, "token": "opaque-value"},
        {**PROPOSE_BODY, "javascript": "alert(1)"},
        {
            **PROPOSE_BODY,
            "changes": [{"cell": "A1", "kind": "set_value", "value": float("nan")}],
        },
    ],
)
def test_adapter_rejects_authority_fields_code_and_non_finite_numbers(edit_web, body) -> None:
    service, adapter = edit_web

    response = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "invalid-key"},
        json_body=body,
    )

    assert response.status_code == 400
    assert response.body == {
        "schema_version": "audit_workbook_edit_http_error.v1",
        "error": {
            "code": "INVALID_REQUEST",
            "message": "The JSON request does not match the workbook edit contract.",
        },
    }
    assert response.headers["Cache-Control"] == "no-store, private"
    assert service.calls == []


def test_adapter_rejects_a_schema_valid_body_above_the_http_payload_bound(edit_web) -> None:
    service, adapter = edit_web
    body = {
        "session_id": "office-session-a",
        "changes": [
            {"cell": f"A{index}", "kind": "set_value", "value": "x" * 32767}
            for index in range(1, 34)
        ],
    }
    jsonschema.validate(body, PROPOSE_REQUEST_SCHEMA)

    response = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "oversized"},
        json_body=body,
    )

    assert response.status_code == 400
    assert response.body["error"]["code"] == "INVALID_REQUEST"
    assert service.calls == []


def test_adapter_rejects_deep_direct_objects_without_recursive_validation_failure(
    edit_web,
) -> None:
    service, adapter = edit_web
    deep_body: dict[str, object] = {}
    cursor = deep_body
    for _ in range(1_100):
        nested: dict[str, object] = {}
        cursor["nested"] = nested
        cursor = nested

    request_response = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "deep-direct-request"},
        json_body=deep_body,
    )
    assert request_response.status_code == 400
    assert request_response.body["error"]["code"] == "INVALID_REQUEST"
    assert service.calls == []

    class DeepReceipt:
        def to_dict(self):
            return deep_body

    service.propose = lambda **kwargs: SimpleNamespace(
        receipt=DeepReceipt(),
        replayed=False,
    )
    response = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "deep-direct-response"},
        json_body=PROPOSE_BODY,
    )
    assert response.status_code == 500
    assert response.body["error"]["code"] == "INTERNAL_ERROR"


def test_adapter_normalizes_service_and_unexpected_failures_without_leaking_details(edit_web) -> None:
    service, adapter = edit_web
    service.failure = WorkbookEditServiceError(
        "STALE_REVISION",
        "/srv/private/client.xlsx token=must-not-leak",
        status_code=409,
    )
    stale = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "stale"},
        json_body=PROPOSE_BODY,
    )
    service.failure = RuntimeError("/srv/private/client.xlsx private diagnostic")
    internal = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "internal"},
        json_body=PROPOSE_BODY,
    )

    assert stale.status_code == 409
    assert stale.body["error"]["code"] == "STALE_REVISION"
    assert "must-not-leak" not in str(stale.body)
    assert internal.status_code == 500
    assert internal.body["error"]["code"] == "INTERNAL_ERROR"
    assert "/srv/private" not in str(internal.body)
    assert "private diagnostic" not in str(internal.body)


def test_adapter_runs_the_real_session_bound_service_lifecycle() -> None:
    binding = WorkbookSessionBinding(
        session_id="office-session-a",
        tenant_id=PRINCIPAL.tenant_id,
        subject_id=PRINCIPAL.subject_id,
        bundle_id="bundle-a",
        snapshot_id="1" * 64,
        workbook_sha256="2" * 64,
        revision_id="revision-a",
        sheet="C",
        worksheet_id="worksheet-a",
        workbook_instance_id="workbook-instance-a",
    )
    service = WorkbookEditService(
        sessions=InMemoryWorkbookSessionRepository([binding]),
        edits=InMemoryWorkbookEditRepository(),
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    adapter = AuditWorkbookEditHttpAdapter(service)

    proposed = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "real-propose"},
        json_body=PROPOSE_BODY,
    )
    assert proposed.status_code == 201
    workflow_id = proposed.body["receipt"]["workflow_id"]

    previewed = adapter.preview(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        headers={"Idempotency-Key": "real-preview"},
        json_body=PREVIEW_BODY,
    )
    assert previewed.status_code == 201
    preview = previewed.body["receipt"]["details"]["preview"]

    approved = adapter.approve(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        preview_id=preview["preview_ref"],
        headers={"Idempotency-Key": "real-approve"},
        json_body={
            "preview_sha256": preview["preview_sha256"],
            "confirmed": True,
        },
    )
    assert approved.status_code == 200
    assert approved.body["receipt"]["state"] == "approved"

    claimed = adapter.claim_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        headers={"Idempotency-Key": "real-claim"},
        json_body=CLAIM_BODY,
    )
    assert claimed.status_code == 201
    claim_details = claimed.body["receipt"]["details"]

    started = adapter.mark_apply_started(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=claim_details["execution_id"],
        headers={"Idempotency-Key": "real-start"},
        json_body={
            "fence": claim_details["fence"],
            "challenge": claim_details["challenge"],
        },
    )
    assert started.status_code == 200
    assert started.body["receipt"]["state"] == "apply_started"

    verified = adapter.verify_execution(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        execution_id=claim_details["execution_id"],
        headers={"Idempotency-Key": "real-verify"},
        json_body={
            "fence": claim_details["fence"],
            "challenge": claim_details["challenge"],
            "witness": VERIFY_BODY["witness"],
        },
    )
    assert verified.status_code == 200
    assert verified.body["receipt"]["state"] == "session_verified"
    assert (
        verified.body["receipt"]["details"]["verification"]["application_status"]
        == "applied_session_verified"
    )

    current = adapter.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)
    assert current.status_code == 200
    assert current.body["workflow"]["state"] == "session_verified"
    assert current.body["workflow"]["approval_consumed"] is True
    assert claim_details["challenge"] not in str(current.body)
    assert "challenge_nonce" not in str(current.body)


def test_valid_large_review_workflow_response_stays_below_the_explicit_output_cap() -> None:
    binding = WorkbookSessionBinding(
        session_id="large-session",
        tenant_id=PRINCIPAL.tenant_id,
        subject_id=PRINCIPAL.subject_id,
        bundle_id="large-bundle",
        snapshot_id="3" * 64,
        workbook_sha256="4" * 64,
        revision_id="large-revision",
        sheet="Large",
        worksheet_id="large-worksheet",
        workbook_instance_id="large-workbook-instance",
    )
    service = WorkbookEditService(
        sessions=InMemoryWorkbookSessionRepository([binding]),
        edits=InMemoryWorkbookEditRepository(),
        now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    adapter = AuditWorkbookEditHttpAdapter(service)
    changes = [
        {"cell": f"A{index}", "kind": "set_value", "value": "n" * 875}
        for index in range(1, 101)
    ]
    proposed = adapter.propose(
        principal=PRINCIPAL,
        headers={"Idempotency-Key": "large-propose"},
        json_body={"session_id": "large-session", "changes": changes},
    )
    assert proposed.status_code == 201
    workflow_id = proposed.body["receipt"]["workflow_id"]
    before = [
        {
            **STATE_VALUE,
            "cell": f"A{index}",
            "authored": {"kind": "value", "value": "o" * 875},
            "calculated_value": "o" * 875,
        }
        for index in range(1, 101)
    ]
    previewed = adapter.preview(
        principal=PRINCIPAL,
        workflow_id=workflow_id,
        headers={"Idempotency-Key": "large-preview"},
        json_body={
            "office_revision_id": "large-revision",
            "worksheet_id": "large-worksheet",
            "before": before,
        },
    )
    assert previewed.status_code == 201

    fetched = adapter.get_workflow(principal=PRINCIPAL, workflow_id=workflow_id)
    serialized = json.dumps(
        fetched.body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert fetched.status_code == 200
    assert 600_000 < len(serialized) < edit_web_module._MAX_HTTP_RESPONSE_BYTES


def test_fastapi_import_is_lazy(edit_web, monkeypatch) -> None:
    _, adapter = edit_web
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "fastapi" or name.startswith("fastapi."):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(WorkbookEditWebAdapterUnavailableError, match="FastAPI is optional"):
        create_workbook_edit_fastapi_app(
            adapter,
            principal_dependency=lambda: PRINCIPAL,
        )


def test_fastapi_routes_preserve_strict_headers_and_no_store(edit_web) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    service, adapter = edit_web
    app = create_workbook_edit_fastapi_app(
        adapter,
        principal_dependency=lambda: PRINCIPAL,
    )
    openapi = app.openapi()
    openapi_request = openapi["paths"][
        "/v1/audit/workbook-edit-workflows"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert openapi_request == {
        "$ref": "#/components/schemas/AuditWorkbookEditProposeRequest"
    }
    openapi_response = openapi["paths"][
        "/v1/audit/workbook-edit-workflows"
    ]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
    assert openapi_response == {
        "$ref": "#/components/schemas/AuditWorkbookEditSubmissionResponse"
    }

    def resolve_ref(ref):
        assert ref.startswith("#/")
        current = openapi
        for part in ref[2:].split("/"):
            current = current[part.replace("~1", "/").replace("~0", "~")]
        return current

    visited: set[str] = set()

    def assert_resolvable(value):
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                assert ref.startswith("#/components/schemas/AuditWorkbookEdit")
                resolved = resolve_ref(ref)
                if ref not in visited:
                    visited.add(ref)
                    assert_resolvable(resolved)
            for key, item in value.items():
                if key != "$ref":
                    assert_resolvable(item)
        elif isinstance(value, list):
            for item in value:
                assert_resolvable(item)

    for name, schema in openapi["components"]["schemas"].items():
        if name.startswith("AuditWorkbookEdit"):
            assert_resolvable(schema)
    assert_resolvable(openapi_request)
    assert_resolvable(openapi_response)

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={"Idempotency-Key": "fastapi-propose"},
                    json=PROPOSE_BODY,
                )
                assert created.status_code == 201
                assert created.headers["cache-control"] == "no-store, private"
                workflow_id = created.json()["receipt"]["workflow_id"]

                fetched = await client.get(
                    f"/v1/audit/workbook-edit-workflows/{workflow_id}"
                )
                assert fetched.status_code == 200
                assert fetched.headers["cache-control"] == "no-store, private"

                duplicate = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers=[("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
                    json=PROPOSE_BODY,
                )
                assert duplicate.status_code == 400
                assert duplicate.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"

                unknown = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={"Idempotency-Key": "unknown"},
                    json={**PROPOSE_BODY, "provider": "raw"},
                )
                assert unknown.status_code == 400
                assert unknown.headers["cache-control"] == "no-store, private"
                assert unknown.json()["error"]["code"] == "INVALID_REQUEST"

                malformed = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={"Idempotency-Key": "malformed"},
                    content=b'{"session_id":',
                )
                assert malformed.status_code == 400
                assert malformed.json()["error"]["code"] == "INVALID_REQUEST"

                wrong_content_type = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={
                        "Idempotency-Key": "wrong-content-type",
                        "Content-Type": "text/plain",
                    },
                    content=b"{}",
                )
                assert wrong_content_type.status_code == 400
                assert wrong_content_type.json()["error"]["code"] == "INVALID_REQUEST"

                duplicate_member = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={
                        "Idempotency-Key": "duplicate-member",
                        "Content-Type": "application/json",
                    },
                    content=(
                        b'{"session_id":"office-session-a",'
                        b'"session_id":"office-session-b",'
                        b'"changes":[{"cell":"A1","kind":"clear_contents"}]}'
                    ),
                )
                assert duplicate_member.status_code == 400
                assert duplicate_member.json()["error"]["code"] == "INVALID_REQUEST"
        assert app.state.audit_edit_executor is None
        assert app.state.audit_edit_executor_gate is None

    asyncio.run(scenario())
    assert [call[0] for call in service.calls] == ["propose", "get_workflow"]


def test_fastapi_streams_and_stops_before_an_oversized_body_is_fully_buffered(edit_web) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    service, adapter = edit_web
    app = create_workbook_edit_fastapi_app(
        adapter,
        principal_dependency=lambda: PRINCIPAL,
    )
    streamed: list[int] = []
    prechecked: list[int] = []

    async def oversized_chunks():
        for index in range(3):
            streamed.append(index)
            yield b"x" * 600_000

    async def must_not_be_read():
        prechecked.append(1)
        yield b"{}"

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                streamed_response = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={
                        "Idempotency-Key": "stream-overflow",
                        "Content-Type": "application/json",
                    },
                    content=oversized_chunks(),
                )
                assert streamed_response.status_code == 400
                assert streamed_response.json()["error"]["code"] == "INVALID_REQUEST"

                prechecked_response = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={
                        "Idempotency-Key": "length-overflow",
                        "Content-Type": "application/json",
                        "Content-Length": str(edit_web_module._MAX_HTTP_BODY_BYTES + 1),
                    },
                    content=must_not_be_read(),
                )
                assert prechecked_response.status_code == 400
                assert prechecked_response.json()["error"]["code"] == "INVALID_REQUEST"

    asyncio.run(scenario())
    assert streamed == [0, 1]
    assert prechecked == []
    assert service.calls == []


def test_fastapi_rejects_deep_json_and_ignores_string_delimiters(edit_web) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    service, adapter = edit_web
    app = create_workbook_edit_fastapi_app(
        adapter,
        principal_dependency=lambda: PRINCIPAL,
    )
    deeply_nested = b"[" * 1_100 + b"0" + b"]" * 1_100
    string_value = "[" * 80 + '\\"quoted' + "}" * 80 + "\\"

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                rejected = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={
                        "Idempotency-Key": "deep-raw-json",
                        "Content-Type": "application/json",
                    },
                    content=deeply_nested,
                )
                assert rejected.status_code == 400
                assert rejected.json()["error"]["code"] == "INVALID_REQUEST"

                accepted = await client.post(
                    "/v1/audit/workbook-edit-workflows",
                    headers={"Idempotency-Key": "string-delimiters"},
                    json={
                        "session_id": "office-session-a",
                        "changes": [
                            {"cell": "A1", "kind": "set_value", "value": string_value}
                        ],
                    },
                )
                assert accepted.status_code == 201

    asyncio.run(scenario())
    assert len(deeply_nested) == 2_201
    assert [call[0] for call in service.calls] == ["propose"]
    assert service.calls[0][1]["proposal_input"]["changes"][0]["value"] == string_value


def test_fastapi_normalizes_decoder_recursion_error(edit_web, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    service, adapter = edit_web
    app = create_workbook_edit_fastapi_app(
        adapter,
        principal_dependency=lambda: PRINCIPAL,
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                with monkeypatch.context() as patch:
                    def recursive_decoder(*args, **kwargs):
                        del args, kwargs
                        raise RecursionError("decoder recursion limit")

                    patch.setattr(edit_web_module.json, "loads", recursive_decoder)
                    response = await client.post(
                        "/v1/audit/workbook-edit-workflows",
                        headers={
                            "Idempotency-Key": "decoder-recursion",
                            "Content-Type": "application/json",
                        },
                        content=b"{}",
                    )
                assert response.status_code == 400
                assert response.json()["error"]["code"] == "INVALID_REQUEST"

    asyncio.run(scenario())
    assert service.calls == []


def test_fastapi_cancelled_request_keeps_worker_slot_until_principal_finishes(edit_web) -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    service, adapter = edit_web
    started = threading.Event()
    release = threading.Event()

    def slow_principal():
        started.set()
        release.wait(timeout=5)
        return PRINCIPAL

    app = create_workbook_edit_fastapi_app(
        adapter,
        principal_dependency=slow_principal,
        executor_workers=1,
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                request = asyncio.create_task(
                    client.post(
                        "/v1/audit/workbook-edit-workflows",
                        headers={"Idempotency-Key": "cancelled"},
                        json=PROPOSE_BODY,
                    )
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set()
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                await asyncio.sleep(0)

                gate = app.state.audit_edit_executor_gate
                assert gate._value == 1
                assert service.calls == []
                release.set()
                for _ in range(100):
                    if gate._value == 2:
                        break
                    await asyncio.sleep(0.01)
                assert gate._value == 2

    try:
        asyncio.run(scenario())
    finally:
        release.set()
