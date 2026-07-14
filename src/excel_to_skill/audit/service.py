"""Web-ready service boundary for persistent audit conversations.

The service accepts only opaque public identifiers.  A caller-provided repository resolves a
``bundle_id`` to an immutable, server-owned package snapshot; local package and runtime paths
never appear in commands or receipts.  Public thread IDs are mapped into deterministic
principal-scoped runtime IDs before reaching graph persistence.  HTTP frameworks are deliberately
kept out of this module.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, ContextManager, Literal, Mapping, Protocol

import jsonschema

from ..resources import SCHEMA_DIR
from .conversation import (
    AuditConversationBundleChangedError,
    AuditConversationError,
    run_audit_conversation_turn,
)


_OPAQUE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_AGGREGATE_ID_RE = _SHA256_RE
_MAX_QUESTION_LENGTH = 12_000
_MAX_IDEMPOTENCY_KEY_LENGTH = 200
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "path",
        "package_path",
        "runtime_root",
        "source_path",
        "file_path",
        "internal_thread_id",
        "runtime_thread_id",
    }
)


class AuditConversationServiceError(RuntimeError):
    """A stable, client-safe service error."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class BundleSnapshotNotFoundError(LookupError):
    """The bundle repository cannot expose the requested snapshot to this principal."""


class ConversationArtifactRepositoryError(RuntimeError):
    """The service-level conversation repository could not safely complete an operation."""


class ThreadBundleConflictError(ConversationArtifactRepositoryError):
    """A public conversation thread is already bound to another exact snapshot or scope."""


class IdempotencyConflictError(ConversationArtifactRepositoryError):
    """An idempotency key is already claimed by a different command."""


class IdempotencyClaimError(ConversationArtifactRepositoryError):
    """A pending idempotency claim cannot be completed or released by this owner."""


def _require_opaque_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an opaque identifier")
    return value


def _require_optional_text(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


@dataclass(frozen=True)
class ServicePrincipal:
    """Identity supplied by the hosting application's authentication boundary."""

    tenant_id: str
    subject_id: str

    def __post_init__(self) -> None:
        _require_opaque_id(self.tenant_id, field="tenant_id")
        _require_opaque_id(self.subject_id, field="subject_id")

    @property
    def scope(self) -> tuple[str, str]:
        return (self.tenant_id, self.subject_id)


@dataclass(frozen=True)
class BundleScopeBinding:
    """Optional server-owned restriction for one published bundle's conversation root."""

    sheet: str | None = None
    aggregate_id: str | None = None

    def __post_init__(self) -> None:
        sheet = _require_optional_text(self.sheet, field="sheet", maximum=31)
        if sheet is not None and any(character in sheet for character in "[]:*?/\\"):
            raise ValueError("sheet is not a valid Excel sheet name")
        if self.aggregate_id is not None and (
            not isinstance(self.aggregate_id, str)
            or _AGGREGATE_ID_RE.fullmatch(self.aggregate_id) is None
        ):
            raise ValueError("aggregate_id must be a sha256 identifier")
        if sheet is not None and self.aggregate_id is not None:
            raise ValueError("sheet and aggregate_id are mutually exclusive")
        object.__setattr__(self, "sheet", sheet)


@dataclass(frozen=True)
class BundleSnapshot:
    """One immutable server-owned package/runtime mapping.

    ``snapshot_id`` is a strong repository revision identifier.  Repositories must not reuse it
    after changing package content and must never derive either path from request data.
    """

    bundle_id: str
    snapshot_id: str
    package_path: Path
    runtime_root: Path
    workbook_source_provider: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    scope_binding: BundleScopeBinding | None = None
    commit_lock_root: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_opaque_id(self.bundle_id, field="bundle_id")
        if not isinstance(self.snapshot_id, str) or _SHA256_RE.fullmatch(self.snapshot_id) is None:
            raise ValueError("snapshot_id must be a sha256 identifier")
        package_path = Path(self.package_path)
        runtime_root = Path(self.runtime_root)
        if not package_path.is_absolute() or not runtime_root.is_absolute():
            raise ValueError("bundle repository paths must be absolute server-owned paths")
        if self.scope_binding is not None and not isinstance(
            self.scope_binding, BundleScopeBinding
        ):
            raise ValueError("scope_binding must be a BundleScopeBinding")
        commit_lock_root = (
            None if self.commit_lock_root is None else Path(self.commit_lock_root)
        )
        if commit_lock_root is not None and not commit_lock_root.is_absolute():
            raise ValueError("commit_lock_root must be an absolute server-owned path")
        object.__setattr__(self, "package_path", package_path)
        object.__setattr__(self, "runtime_root", runtime_root)
        object.__setattr__(self, "commit_lock_root", commit_lock_root)


class BundleSnapshotRepository(Protocol):
    """Resolve an opaque ID within the host application's authorization boundary."""

    def resolve(
        self,
        *,
        principal: ServicePrincipal,
        bundle_id: str,
    ) -> BundleSnapshot:
        """Return one immutable server-owned snapshot or raise ``BundleSnapshotNotFoundError``."""


@dataclass(frozen=True)
class ConversationTurnCommand:
    """Strict public command; it intentionally has no filesystem or provider fields."""

    bundle_id: str
    question: str
    thread_id: str | None = None
    sheet: str | None = None
    aggregate_id: str | None = None
    standards_research: bool = False
    procedure_planning: bool = False
    workbook_inspection: bool = False

    def __post_init__(self) -> None:
        _require_opaque_id(self.bundle_id, field="bundle_id")
        if self.thread_id is not None:
            _require_opaque_id(self.thread_id, field="thread_id")
        question = _require_optional_text(
            self.question,
            field="question",
            maximum=_MAX_QUESTION_LENGTH,
        )
        if question is None:
            raise ValueError("question is required")
        sheet = _require_optional_text(self.sheet, field="sheet", maximum=31)
        if sheet is not None and any(character in sheet for character in "[]:*?/\\"):
            raise ValueError("sheet is not a valid Excel sheet name")
        if self.aggregate_id is not None and (
            not isinstance(self.aggregate_id, str)
            or _AGGREGATE_ID_RE.fullmatch(self.aggregate_id) is None
        ):
            raise ValueError("aggregate_id must be a sha256 identifier")
        if sheet is not None and self.aggregate_id is not None:
            raise ValueError("sheet and aggregate_id are mutually exclusive")
        if not isinstance(self.standards_research, bool):
            raise ValueError("standards_research must be boolean")
        if not isinstance(self.procedure_planning, bool):
            raise ValueError("procedure_planning must be boolean")
        if not isinstance(self.workbook_inspection, bool):
            raise ValueError("workbook_inspection must be boolean")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "aggregate_id": self.aggregate_id,
            "bundle_id": self.bundle_id,
            "procedure_planning": self.procedure_planning,
            "question": self.question,
            "sheet": self.sheet,
            "standards_research": self.standards_research,
            "thread_id": self.thread_id,
            "workbook_inspection": self.workbook_inspection,
        }


@dataclass(frozen=True)
class ThreadBundleBinding:
    bundle_id: str
    snapshot_id: str
    sheet: str | None
    aggregate_id: str | None


@dataclass(frozen=True)
class ConversationTurnReceipt:
    """Immutable public result descriptor; no repository path is serializable from here."""

    request_id: str
    bundle_id: str
    snapshot_id: str
    thread_id: str
    result: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "audit_conversation_service_receipt.v1",
            "request_id": self.request_id,
            "bundle_id": self.bundle_id,
            "snapshot_id": self.snapshot_id,
            "thread_id": self.thread_id,
            "result": copy.deepcopy(dict(self.result)),
        }


@dataclass(frozen=True)
class ConversationTurnSubmission:
    receipt: ConversationTurnReceipt
    replayed: bool


@dataclass(frozen=True)
class ConversationTurnClaim:
    """Result of atomically claiming one principal-scoped idempotency key.

    Only a newly acquired claim returns its owner token.  Pending claims deliberately reveal no
    owner material, and completed claims expose only the immutable public receipt.
    """

    state: Literal["claimed", "pending", "completed"]
    receipt: ConversationTurnReceipt | None = None
    claim_token: str | None = None

    def __post_init__(self) -> None:
        if self.state == "claimed":
            if not isinstance(self.claim_token, str) or not self.claim_token:
                raise ValueError("a claimed turn requires its owner token")
            if self.receipt is not None:
                raise ValueError("a claimed turn cannot already have a receipt")
            return
        if self.claim_token is not None:
            raise ValueError("non-owner claim states cannot expose an owner token")
        if self.state == "completed":
            if not isinstance(self.receipt, ConversationTurnReceipt):
                raise ValueError("a completed turn requires its receipt")
            return
        if self.state != "pending" or self.receipt is not None:
            raise ValueError("invalid idempotency claim state")


class ConversationArtifactRepository(Protocol):
    """Persist service receipts, idempotency witnesses, and exact thread bindings."""

    def claim(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        request_id: str,
        claim_token: str,
    ) -> ConversationTurnClaim:
        """Atomically acquire, observe, or replay one idempotency key.

        Raise ``IdempotencyConflictError`` when the key belongs to another command.  A
        ``pending`` result means another owner may already have persisted the runtime turn, so a
        caller must not execute it again.
        """

    def get_by_idempotency(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
    ) -> tuple[str, ConversationTurnReceipt] | None:
        """Return ``(command_sha256, receipt)`` when the key was completed."""

    def get_by_request_id(
        self,
        *,
        principal: ServicePrincipal,
        request_id: str,
    ) -> ConversationTurnReceipt | None:
        """Load a completed receipt within the principal's repository scope."""

    def bind_thread(
        self,
        *,
        principal: ServicePrincipal,
        thread_id: str,
        binding: ThreadBundleBinding,
    ) -> None:
        """Atomically create or verify an exact thread-to-snapshot/scope binding."""

    def publish(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        request_id: str,
        claim_token: str,
        receipt: ConversationTurnReceipt,
    ) -> None:
        """Atomically complete the matching owned claim and publish its receipt."""

    def abort(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        request_id: str,
        claim_token: str,
    ) -> None:
        """Release an owned pending claim only when runtime execution has not started."""


@dataclass
class _InMemoryIdempotencyEntry:
    command_sha256: str
    request_id: str
    state: Literal["pending", "completed"]
    claim_token: str | None
    receipt: ConversationTurnReceipt | None


class InMemoryConversationArtifactRepository:
    """Thread-safe reference implementation for tests and single-process prototypes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._idempotency: dict[
            tuple[tuple[str, str], str], _InMemoryIdempotencyEntry
        ] = {}
        self._requests: dict[
            tuple[tuple[str, str], str], ConversationTurnReceipt
        ] = {}
        self._threads: dict[
            tuple[tuple[str, str], str], ThreadBundleBinding
        ] = {}

    @staticmethod
    def _copy_receipt(receipt: ConversationTurnReceipt) -> ConversationTurnReceipt:
        return ConversationTurnReceipt(
            request_id=receipt.request_id,
            bundle_id=receipt.bundle_id,
            snapshot_id=receipt.snapshot_id,
            thread_id=receipt.thread_id,
            result=copy.deepcopy(dict(receipt.result)),
        )

    def get_by_idempotency(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
    ) -> tuple[str, ConversationTurnReceipt] | None:
        with self._lock:
            found = self._idempotency.get((principal.scope, idempotency_key))
            if found is None or found.state != "completed" or found.receipt is None:
                return None
            return found.command_sha256, self._copy_receipt(found.receipt)

    def claim(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        request_id: str,
        claim_token: str,
    ) -> ConversationTurnClaim:
        with self._lock:
            key = (principal.scope, idempotency_key)
            found = self._idempotency.get(key)
            if found is None:
                self._idempotency[key] = _InMemoryIdempotencyEntry(
                    command_sha256=command_sha256,
                    request_id=request_id,
                    state="pending",
                    claim_token=claim_token,
                    receipt=None,
                )
                return ConversationTurnClaim(
                    state="claimed",
                    claim_token=claim_token,
                )
            if found.command_sha256 != command_sha256 or found.request_id != request_id:
                raise IdempotencyConflictError(
                    "idempotency key was already claimed by another command"
                )
            if found.state == "completed":
                if found.receipt is None:
                    raise ConversationArtifactRepositoryError(
                        "completed idempotency entry has no receipt"
                    )
                return ConversationTurnClaim(
                    state="completed",
                    receipt=self._copy_receipt(found.receipt),
                )
            if found.claim_token == claim_token:
                return ConversationTurnClaim(
                    state="claimed",
                    claim_token=claim_token,
                )
            return ConversationTurnClaim(state="pending")

    def get_by_request_id(
        self,
        *,
        principal: ServicePrincipal,
        request_id: str,
    ) -> ConversationTurnReceipt | None:
        with self._lock:
            found = self._requests.get((principal.scope, request_id))
            return None if found is None else self._copy_receipt(found)

    def bind_thread(
        self,
        *,
        principal: ServicePrincipal,
        thread_id: str,
        binding: ThreadBundleBinding,
    ) -> None:
        with self._lock:
            key = (principal.scope, thread_id)
            existing = self._threads.get(key)
            if existing is not None and existing != binding:
                raise ThreadBundleConflictError(
                    "thread is already bound to another bundle snapshot or scope"
                )
            self._threads[key] = binding

    def publish(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        request_id: str,
        claim_token: str,
        receipt: ConversationTurnReceipt,
    ) -> None:
        with self._lock:
            idem_key = (principal.scope, idempotency_key)
            request_key = (principal.scope, receipt.request_id)
            existing = self._idempotency.get(idem_key)
            if existing is None:
                raise IdempotencyClaimError(
                    "idempotency key was not claimed before publication"
                )
            if (
                existing.command_sha256 != command_sha256
                or existing.request_id != request_id
                or receipt.request_id != request_id
            ):
                raise IdempotencyClaimError(
                    "publication does not match the pending idempotency claim"
                )
            if existing.state == "completed":
                if (
                    existing.receipt is None
                    or existing.receipt.to_dict() != receipt.to_dict()
                ):
                    raise IdempotencyClaimError(
                        "idempotency key was already published with another result"
                    )
                return
            if existing.claim_token != claim_token:
                raise IdempotencyClaimError(
                    "publication is not owned by the pending claimant"
                )
            existing_request = self._requests.get(request_key)
            if existing_request is not None and existing_request.to_dict() != receipt.to_dict():
                raise ConversationArtifactRepositoryError(
                    "request identifier collision"
                )
            copied = self._copy_receipt(receipt)
            self._idempotency[idem_key] = _InMemoryIdempotencyEntry(
                command_sha256=command_sha256,
                request_id=request_id,
                state="completed",
                claim_token=None,
                receipt=copied,
            )
            self._requests[request_key] = copied

    def abort(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        request_id: str,
        claim_token: str,
    ) -> None:
        with self._lock:
            key = (principal.scope, idempotency_key)
            existing = self._idempotency.get(key)
            if existing is None:
                raise IdempotencyClaimError("idempotency claim does not exist")
            if (
                existing.state != "pending"
                or existing.command_sha256 != command_sha256
                or existing.request_id != request_id
                or existing.claim_token != claim_token
            ):
                raise IdempotencyClaimError(
                    "idempotency claim cannot be released by this claimant"
                )
            del self._idempotency[key]


class TurnLock(Protocol):
    """Host-provided local or distributed lock for one internal conversation thread."""

    def hold(
        self,
        *,
        principal: ServicePrincipal,
        thread_id: str,
    ) -> ContextManager[object]:
        """Return a context manager that serializes this principal/thread pair."""


class NoopTurnLock:
    """Explicit opt-out; suitable only when another layer already serializes requests."""

    def hold(
        self,
        *,
        principal: ServicePrincipal,
        thread_id: str,
    ) -> ContextManager[object]:
        del principal, thread_id
        return nullcontext()


class InMemoryTurnLock:
    """Process-local keyed lock with bounded entry lifetime."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[tuple[tuple[str, str], str], tuple[threading.RLock, int]] = {}

    @contextmanager
    def hold(
        self,
        *,
        principal: ServicePrincipal,
        thread_id: str,
    ):
        key = (principal.scope, thread_id)
        with self._guard:
            lock, users = self._entries.get(key, (threading.RLock(), 0))
            self._entries[key] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                current_lock, current_users = self._entries[key]
                if current_users == 1:
                    del self._entries[key]
                else:
                    self._entries[key] = (current_lock, current_users - 1)


def _canonical_digest(value: Mapping[str, object]) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise AuditConversationServiceError(
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must be a non-empty visible string.",
            status_code=400,
        )
    return value


def _derived_id(prefix: str, principal: ServicePrincipal, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        (
            prefix
            + "\0"
            + principal.tenant_id
            + "\0"
            + principal.subject_id
            + "\0"
            + idempotency_key
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest[:48]}"


def _runtime_thread_id(
    *,
    principal: ServicePrincipal,
    public_thread_id: str,
) -> str:
    """Map a public thread into a deterministic, non-reversible principal namespace."""

    digest = hashlib.sha256(
        (
            "audit-conversation-runtime-thread-v1"
            + "\0"
            + principal.tenant_id
            + "\0"
            + principal.subject_id
            + "\0"
            + public_thread_id
        ).encode("utf-8")
    ).hexdigest()
    return f"runtime-thread-{digest[:48]}"


@lru_cache(maxsize=1)
def _turn_result_schema() -> dict[str, object]:
    try:
        value = json.loads(
            (SCHEMA_DIR / "audit_conversation_turn_result.schema.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditConversationServiceError(
            "INVALID_TURN_RESULT",
            "The conversation result contract is unavailable.",
            status_code=500,
        ) from exc
    if not isinstance(value, dict):
        raise AuditConversationServiceError(
            "INVALID_TURN_RESULT",
            "The conversation result contract is unavailable.",
            status_code=500,
        )
    return value


def _assert_public_result(
    value: object,
    *,
    snapshot: BundleSnapshot,
    runtime_thread_id: str,
    public_thread_id: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AuditConversationServiceError(
            "INVALID_TURN_RESULT",
            "The conversation runtime returned an invalid result.",
            status_code=500,
        )
    try:
        jsonschema.validate(value, _turn_result_schema())
    except (jsonschema.ValidationError, jsonschema.SchemaError) as exc:
        raise AuditConversationServiceError(
            "INVALID_TURN_RESULT",
            "The conversation runtime returned an invalid result.",
            status_code=500,
        ) from exc
    usage = value["usage"]
    requests = usage["requests"]
    if (
        usage["request_count"] != len(requests)
        or usage["input_tokens"]
        != sum(item["input_tokens"] for item in requests)
        or usage["output_tokens"]
        != sum(item["output_tokens"] for item in requests)
        or usage["total_tokens"]
        != sum(item["total_tokens"] for item in requests)
        or usage["total_tokens"]
        != usage["input_tokens"] + usage["output_tokens"]
        or any(
            item["event_id"] != f"request:{index}"
            or item["total_tokens"]
            != item["input_tokens"] + item["output_tokens"]
            for index, item in enumerate(requests, 1)
        )
    ):
        raise AuditConversationServiceError(
            "INVALID_TURN_RESULT",
            "The conversation runtime returned invalid usage metadata.",
            status_code=500,
        )
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AuditConversationServiceError(
            "INVALID_TURN_RESULT",
            "The conversation runtime returned an invalid result.",
            status_code=500,
        ) from exc
    if str(snapshot.package_path) in serialized or str(snapshot.runtime_root) in serialized:
        raise AuditConversationServiceError(
            "PATH_DISCLOSURE_BLOCKED",
            "The conversation result failed the public response boundary.",
            status_code=500,
        )

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if _FORBIDDEN_PUBLIC_KEYS.intersection(item):
                raise AuditConversationServiceError(
                    "PATH_DISCLOSURE_BLOCKED",
                    "The conversation result failed the public response boundary.",
                    status_code=500,
                )
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if value.get("thread_id") != runtime_thread_id:
        raise AuditConversationServiceError(
            "INVALID_TURN_RESULT",
            "The conversation runtime returned an invalid thread binding.",
            status_code=500,
        )
    public_value = json.loads(serialized)
    public_value["thread_id"] = public_thread_id
    public_serialized = json.dumps(
        public_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if runtime_thread_id != public_thread_id and runtime_thread_id in public_serialized:
        raise AuditConversationServiceError(
            "INTERNAL_IDENTIFIER_DISCLOSURE_BLOCKED",
            "The conversation result failed the public response boundary.",
            status_code=500,
        )
    return json.loads(public_serialized)


class AuditConversationService:
    """Synchronous application service over ``run_audit_conversation_turn``."""

    def __init__(
        self,
        *,
        bundles: BundleSnapshotRepository,
        artifacts: ConversationArtifactRepository,
        model: str,
        limit: int = 100,
        max_steps: int = 6,
        checkpointer=None,
        turn_lock: TurnLock | None = None,
        client=None,
        client_factory=None,
        standards_retriever=None,
        standards_retriever_factory=None,
        runner: Callable[..., dict] = run_audit_conversation_turn,
        eprint=None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 12:
            raise ValueError("max_steps must be an integer from 1 to 12")
        self._bundles = bundles
        self._artifacts = artifacts
        self._model = model.strip()
        self._limit = limit
        self._max_steps = max_steps
        self._checkpointer = checkpointer
        self._turn_lock = turn_lock or InMemoryTurnLock()
        self._client = client
        self._client_factory = client_factory
        self._standards_retriever = standards_retriever
        self._standards_retriever_factory = standards_retriever_factory
        self._runner = runner
        self._eprint = eprint

    def submit_turn(
        self,
        *,
        principal: ServicePrincipal,
        command: ConversationTurnCommand,
        idempotency_key: str,
    ) -> ConversationTurnSubmission:
        if not isinstance(principal, ServicePrincipal):
            raise AuditConversationServiceError(
                "INVALID_PRINCIPAL",
                "The host application did not provide a valid principal.",
                status_code=500,
            )
        if not isinstance(command, ConversationTurnCommand):
            raise AuditConversationServiceError(
                "INVALID_REQUEST",
                "The conversation command is invalid.",
                status_code=400,
            )
        key = _idempotency_key(idempotency_key)
        command_sha256 = _canonical_digest(command.canonical_payload())
        public_thread_id = command.thread_id or _derived_id("thread", principal, key)
        runtime_thread_id = _runtime_thread_id(
            principal=principal,
            public_thread_id=public_thread_id,
        )
        request_id = _derived_id("turn", principal, key)
        proposed_claim_token = secrets.token_hex(32)

        try:
            claim = self._artifacts.claim(
                principal=principal,
                idempotency_key=key,
                command_sha256=command_sha256,
                request_id=request_id,
                claim_token=proposed_claim_token,
            )
            if not isinstance(claim, ConversationTurnClaim):
                raise ConversationArtifactRepositoryError(
                    "idempotency repository returned an invalid claim"
                )
            if claim.state == "completed":
                if claim.receipt is None:
                    raise ConversationArtifactRepositoryError(
                        "completed idempotency claim has no receipt"
                    )
                return ConversationTurnSubmission(
                    receipt=claim.receipt,
                    replayed=True,
                )
            if claim.state == "pending":
                raise AuditConversationServiceError(
                    "TURN_IN_PROGRESS",
                    "A turn with this Idempotency-Key is already in progress.",
                    status_code=409,
                )
            claim_token = claim.claim_token
            if claim.state != "claimed" or claim_token is None:
                raise ConversationArtifactRepositoryError(
                    "idempotency repository returned an invalid owned claim"
                )

            runtime_started = False
            try:
                turn_lock = self._turn_lock.hold(
                    principal=principal,
                    thread_id=runtime_thread_id,
                )
                with turn_lock:
                    try:
                        snapshot = self._bundles.resolve(
                            principal=principal,
                            bundle_id=command.bundle_id,
                        )
                    except BundleSnapshotNotFoundError as exc:
                        raise AuditConversationServiceError(
                            "BUNDLE_NOT_FOUND",
                            "The requested bundle snapshot was not found.",
                            status_code=404,
                        ) from exc
                    if (
                        not isinstance(snapshot, BundleSnapshot)
                        or snapshot.bundle_id != command.bundle_id
                    ):
                        raise AuditConversationServiceError(
                            "INVALID_BUNDLE_MAPPING",
                            "The server bundle mapping is invalid.",
                            status_code=500,
                        )
                    if not snapshot.package_path.is_dir():
                        raise AuditConversationServiceError(
                            "BUNDLE_UNAVAILABLE",
                            "The requested bundle snapshot is unavailable.",
                            status_code=503,
                        )

                    effective_sheet = command.sheet
                    effective_aggregate_id = command.aggregate_id
                    if snapshot.scope_binding is not None:
                        bound = snapshot.scope_binding
                        if (
                            command.sheet is not None
                            or command.aggregate_id is not None
                        ) and (
                            command.sheet != bound.sheet
                            or command.aggregate_id != bound.aggregate_id
                        ):
                            raise AuditConversationServiceError(
                                "BUNDLE_SCOPE_CONFLICT",
                                "The requested conversation scope is not published by this bundle.",
                                status_code=409,
                            )
                        effective_sheet = bound.sheet
                        effective_aggregate_id = bound.aggregate_id

                    binding = ThreadBundleBinding(
                        bundle_id=snapshot.bundle_id,
                        snapshot_id=snapshot.snapshot_id,
                        sheet=effective_sheet,
                        aggregate_id=effective_aggregate_id,
                    )
                    self._artifacts.bind_thread(
                        principal=principal,
                        thread_id=public_thread_id,
                        binding=binding,
                    )
                    # From this point the runner may have written checkpoints or private turn
                    # objects even when it later raises.  Never auto-abort this claim: a retry
                    # must fail closed as pending until repository-level reconciliation proves
                    # that re-execution is safe.
                    runtime_started = True
                    try:
                        runner_arguments = {
                            "model": self._model,
                            "question": command.question,
                            "thread_id": runtime_thread_id,
                            "sheet": effective_sheet,
                            "aggregate_id": effective_aggregate_id,
                            "limit": self._limit,
                            "max_steps": self._max_steps,
                            "client": self._client,
                            "client_factory": self._client_factory,
                            "standards_research": command.standards_research,
                            "procedure_planning": command.procedure_planning,
                            "workbook_inspection": command.workbook_inspection,
                            "workbook_source_provider": snapshot.workbook_source_provider,
                            "standards_retriever": self._standards_retriever,
                            "standards_retriever_factory": self._standards_retriever_factory,
                            "checkpointer": self._checkpointer,
                            "runtime_root": snapshot.runtime_root,
                            "eprint": self._eprint,
                        }
                        if snapshot.commit_lock_root is not None:
                            runner_arguments["commit_lock_root"] = snapshot.commit_lock_root
                        raw_result = self._runner(snapshot.package_path, **runner_arguments)
                    except AuditConversationBundleChangedError as exc:
                        raise AuditConversationServiceError(
                            "BUNDLE_CHANGED",
                            "The conversation is bound to a different bundle snapshot.",
                            status_code=409,
                        ) from exc
                    except AuditConversationError as exc:
                        raise AuditConversationServiceError(
                            "TURN_REJECTED",
                            "The audit conversation turn could not be completed.",
                            status_code=422,
                        ) from exc
                    result = _assert_public_result(
                        raw_result,
                        snapshot=snapshot,
                        runtime_thread_id=runtime_thread_id,
                        public_thread_id=public_thread_id,
                    )
                    receipt = ConversationTurnReceipt(
                        request_id=request_id,
                        bundle_id=snapshot.bundle_id,
                        snapshot_id=snapshot.snapshot_id,
                        thread_id=public_thread_id,
                        result=result,
                    )
                    self._artifacts.publish(
                        principal=principal,
                        idempotency_key=key,
                        command_sha256=command_sha256,
                        request_id=request_id,
                        claim_token=claim_token,
                        receipt=receipt,
                    )
                    return ConversationTurnSubmission(
                        receipt=receipt,
                        replayed=False,
                    )
            except Exception:
                if not runtime_started:
                    self._artifacts.abort(
                        principal=principal,
                        idempotency_key=key,
                        command_sha256=command_sha256,
                        request_id=request_id,
                        claim_token=claim_token,
                    )
                raise
        except AuditConversationServiceError:
            raise
        except IdempotencyConflictError as exc:
            raise AuditConversationServiceError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used for a different request.",
                status_code=409,
            ) from exc
        except ThreadBundleConflictError as exc:
            raise AuditConversationServiceError(
                "THREAD_BUNDLE_CONFLICT",
                "The conversation thread is already bound to another bundle snapshot or scope.",
                status_code=409,
            ) from exc
        except ConversationArtifactRepositoryError as exc:
            raise AuditConversationServiceError(
                "SERVICE_STORAGE_UNAVAILABLE",
                "The conversation service repository is unavailable.",
                status_code=503,
            ) from exc
        except Exception as exc:  # provider, lock, and repository implementation boundary
            raise AuditConversationServiceError(
                "TURN_FAILED",
                "The conversation turn failed at the service boundary.",
                status_code=500,
            ) from exc

    def get_turn(
        self,
        *,
        principal: ServicePrincipal,
        request_id: str,
    ) -> ConversationTurnReceipt:
        if not isinstance(principal, ServicePrincipal):
            raise AuditConversationServiceError(
                "INVALID_PRINCIPAL",
                "The host application did not provide a valid principal.",
                status_code=500,
            )
        try:
            normalized = _require_opaque_id(request_id, field="request_id")
        except ValueError as exc:
            raise AuditConversationServiceError(
                "INVALID_REQUEST_ID",
                "The request identifier is invalid.",
                status_code=400,
            ) from exc
        try:
            receipt = self._artifacts.get_by_request_id(
                principal=principal,
                request_id=normalized,
            )
        except ConversationArtifactRepositoryError as exc:
            raise AuditConversationServiceError(
                "SERVICE_STORAGE_UNAVAILABLE",
                "The conversation service repository is unavailable.",
                status_code=503,
            ) from exc
        if receipt is None:
            raise AuditConversationServiceError(
                "TURN_NOT_FOUND",
                "The requested conversation turn was not found.",
                status_code=404,
            )
        return receipt


__all__ = [
    "AuditConversationService",
    "AuditConversationServiceError",
    "BundleScopeBinding",
    "BundleSnapshot",
    "BundleSnapshotNotFoundError",
    "BundleSnapshotRepository",
    "ConversationArtifactRepository",
    "ConversationArtifactRepositoryError",
    "ConversationTurnClaim",
    "ConversationTurnCommand",
    "ConversationTurnReceipt",
    "ConversationTurnSubmission",
    "IdempotencyClaimError",
    "IdempotencyConflictError",
    "InMemoryConversationArtifactRepository",
    "InMemoryTurnLock",
    "NoopTurnLock",
    "ServicePrincipal",
    "ThreadBundleBinding",
    "ThreadBundleConflictError",
    "TurnLock",
]
