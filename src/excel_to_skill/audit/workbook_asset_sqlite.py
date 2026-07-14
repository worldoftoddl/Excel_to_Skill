"""Private SQLite catalog for uploaded raw workbook snapshots.

The database is intentionally separate from the Office edit repository.  Each write operation
uses ``BEGIN IMMEDIATE``.  Initial publication inserts the principal-scoped workbook, immutable
raw snapshot, raw-workbook head, and completed idempotency receipt in one transaction.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .service import ServicePrincipal
from .workbook_asset_service import (
    RAW_SNAPSHOT_SCHEMA_VERSION,
    RawWorkbookSnapshot,
    StoredRawWorkbookSnapshot,
    WorkbookAssetClaimError,
    WorkbookAssetCommandClaim,
    WorkbookAssetIdempotencyConflictError,
    WorkbookAssetRepositoryError,
)
from .workbook_snapshot_publication import StoredWorkbookAsset


_SCHEMA_VERSION = 1
_OPAQUE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_WORKBOOK_ID_RE = re.compile(r"\Aworkbook-[0-9a-f]{48}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32,128}\Z")
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "workbook_id",
        "raw_snapshot_id",
        "workbook_sha256",
        "size_bytes",
        "status",
        "origin_kind",
        "prepared_bundle_created",
        "created_at",
    }
)


class SQLiteWorkbookAssetRepository:
    """Restart-safe principal-scoped catalog with leased, fenced upload commands."""

    def __init__(
        self,
        database: Path | str,
        *,
        timeout_seconds: float = 30.0,
        command_claim_ttl_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        candidate = Path(database).expanduser()
        if not candidate.is_absolute():
            raise WorkbookAssetRepositoryError("workbook asset database path must be absolute")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0.1 <= float(timeout_seconds) <= 120.0
        ):
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        if (
            not isinstance(command_claim_ttl_seconds, int)
            or isinstance(command_claim_ttl_seconds, bool)
            or not 1 <= command_claim_ttl_seconds <= 900
        ):
            raise ValueError("command_claim_ttl_seconds must be between 1 and 900")
        self._database = candidate
        self._timeout = float(timeout_seconds)
        self._claim_ttl = command_claim_ttl_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._prepare_database_file()
        try:
            with self._transaction() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, _SCHEMA_VERSION):
                    raise WorkbookAssetRepositoryError(
                        "workbook asset database schema version is unsupported"
                    )
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        except WorkbookAssetRepositoryError:
            raise
        except sqlite3.Error:
            raise WorkbookAssetRepositoryError(
                "workbook asset database initialization failed"
            ) from None

    @property
    def database_path(self) -> Path:
        return self._database

    def claim_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        workbook_id: str,
        claim_token: str,
    ) -> WorkbookAssetCommandClaim:
        tenant_id, subject_id = _principal_scope(principal)
        _idempotency_key(idempotency_key)
        _sha256(command_sha256, field="command_sha256")
        _opaque(command_id, field="command_id")
        _workbook_id(workbook_id)
        _token(claim_token)
        now_epoch = self._clock_epoch()
        expires_epoch = now_epoch + self._claim_ttl
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT command_sha256, command_id, workbook_id, state, claim_token,
                           claim_fence, claim_expires_at, receipt_json
                    FROM upload_commands
                    WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                    """,
                    (tenant_id, subject_id, idempotency_key),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO upload_commands(
                            tenant_id, subject_id, idempotency_key, command_sha256,
                            command_id, workbook_id, state, claim_token, claim_fence,
                            claim_expires_at, receipt_json
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 1, ?, NULL)
                        """,
                        (
                            tenant_id,
                            subject_id,
                            idempotency_key,
                            command_sha256,
                            command_id,
                            workbook_id,
                            claim_token,
                            expires_epoch,
                        ),
                    )
                    return WorkbookAssetCommandClaim(
                        "claimed", claim_token=claim_token, claim_fence=1
                    )
                if row["command_sha256"] != command_sha256:
                    raise WorkbookAssetIdempotencyConflictError("idempotency conflict")
                if row["command_id"] != command_id or row["workbook_id"] != workbook_id:
                    raise WorkbookAssetRepositoryError("upload command identity changed")
                if row["state"] == "completed":
                    if row["receipt_json"] is None:
                        raise WorkbookAssetRepositoryError("completed upload has no receipt")
                    receipt = _decode_snapshot(row["receipt_json"])
                    if receipt.workbook_id != workbook_id:
                        raise WorkbookAssetRepositoryError("upload receipt identity changed")
                    return WorkbookAssetCommandClaim("completed", receipt=receipt)
                if row["state"] != "pending" or not isinstance(
                    row["claim_expires_at"], int
                ):
                    raise WorkbookAssetRepositoryError("upload command state is invalid")
                if row["claim_expires_at"] > now_epoch:
                    return WorkbookAssetCommandClaim("pending")
                old_fence = row["claim_fence"]
                if not isinstance(old_fence, int) or old_fence < 1 or old_fence >= 2**63 - 1:
                    raise WorkbookAssetRepositoryError("upload claim fence is invalid")
                new_fence = old_fence + 1
                changed = connection.execute(
                    """
                    UPDATE upload_commands
                    SET claim_token=?, claim_fence=?, claim_expires_at=?
                    WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                      AND command_sha256=? AND command_id=? AND workbook_id=?
                      AND state='pending' AND claim_fence=? AND claim_expires_at<=?
                    """,
                    (
                        claim_token,
                        new_fence,
                        expires_epoch,
                        tenant_id,
                        subject_id,
                        idempotency_key,
                        command_sha256,
                        command_id,
                        workbook_id,
                        old_fence,
                        now_epoch,
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkbookAssetClaimError("upload claim lease changed")
                return WorkbookAssetCommandClaim(
                    "claimed", claim_token=claim_token, claim_fence=new_fence
                )
        except (
            WorkbookAssetIdempotencyConflictError,
            WorkbookAssetClaimError,
            WorkbookAssetRepositoryError,
        ):
            raise
        except sqlite3.Error:
            raise WorkbookAssetRepositoryError("workbook upload claim failed") from None

    def publish_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
        stored: StoredRawWorkbookSnapshot,
    ) -> RawWorkbookSnapshot:
        tenant_id, subject_id = _principal_scope(principal)
        if not isinstance(stored, StoredRawWorkbookSnapshot):
            raise TypeError("stored must be a StoredRawWorkbookSnapshot")
        snapshot = stored.snapshot
        receipt_json = _encode_snapshot(snapshot)
        try:
            with self._transaction() as connection:
                command = self._require_claim(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                )
                if command["workbook_id"] != snapshot.workbook_id:
                    raise WorkbookAssetClaimError("upload workbook identity changed")
                connection.execute(
                    """
                    INSERT INTO workbooks(tenant_id, subject_id, workbook_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (tenant_id, subject_id, snapshot.workbook_id, snapshot.created_at),
                )
                connection.execute(
                    """
                    INSERT INTO raw_snapshots(
                        tenant_id, subject_id, workbook_id, raw_snapshot_id,
                        workbook_sha256, size_bytes, status, origin_kind,
                        prepared_bundle_created, created_at, asset_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        subject_id,
                        snapshot.workbook_id,
                        snapshot.raw_snapshot_id,
                        snapshot.workbook_sha256,
                        snapshot.size_bytes,
                        snapshot.status,
                        snapshot.origin_kind,
                        int(snapshot.prepared_bundle_created),
                        snapshot.created_at,
                        stored.asset.asset_ref,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO raw_snapshot_heads(
                        tenant_id, subject_id, workbook_id, raw_snapshot_id,
                        head_version, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        tenant_id,
                        subject_id,
                        snapshot.workbook_id,
                        snapshot.raw_snapshot_id,
                        snapshot.created_at,
                    ),
                )
                self._before_publish_commit(connection, stored)
                changed = connection.execute(
                    """
                    UPDATE upload_commands
                    SET state='completed', claim_token=NULL, claim_expires_at=NULL,
                        receipt_json=?
                    WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                      AND command_sha256=? AND command_id=? AND workbook_id=?
                      AND state='pending' AND claim_token=? AND claim_fence=?
                      AND claim_expires_at>?
                    """,
                    (
                        receipt_json,
                        tenant_id,
                        subject_id,
                        idempotency_key,
                        command_sha256,
                        command_id,
                        snapshot.workbook_id,
                        claim_token,
                        claim_fence,
                        self._clock_epoch(),
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkbookAssetClaimError("upload claim changed")
                return _decode_snapshot(receipt_json)
        except (WorkbookAssetClaimError, WorkbookAssetRepositoryError):
            raise
        except sqlite3.IntegrityError:
            raise WorkbookAssetRepositoryError("raw workbook identity already exists") from None
        except sqlite3.Error:
            raise WorkbookAssetRepositoryError("raw workbook publication failed") from None

    def abort_upload(
        self,
        *,
        principal: ServicePrincipal,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
    ) -> None:
        tenant_id, subject_id = _principal_scope(principal)
        try:
            with self._transaction() as connection:
                self._require_claim(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    command_sha256=command_sha256,
                    command_id=command_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                )
                deleted = connection.execute(
                    """
                    DELETE FROM upload_commands
                    WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
                      AND command_sha256=? AND command_id=? AND state='pending'
                      AND claim_token=? AND claim_fence=?
                    """,
                    (
                        tenant_id,
                        subject_id,
                        idempotency_key,
                        command_sha256,
                        command_id,
                        claim_token,
                        claim_fence,
                    ),
                ).rowcount
                if deleted != 1:
                    raise WorkbookAssetClaimError("upload claim mismatch")
        except (WorkbookAssetClaimError, WorkbookAssetRepositoryError):
            raise
        except sqlite3.Error:
            raise WorkbookAssetRepositoryError("workbook upload abort failed") from None

    def get_snapshot(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
        raw_snapshot_id: str,
    ) -> StoredRawWorkbookSnapshot | None:
        tenant_id, subject_id = _principal_scope(principal)
        _workbook_id(workbook_id)
        _sha256(raw_snapshot_id, field="raw_snapshot_id")
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT workbook_sha256, size_bytes, status, origin_kind,
                           prepared_bundle_created, created_at, asset_ref
                    FROM raw_snapshots
                    WHERE tenant_id=? AND subject_id=? AND workbook_id=? AND raw_snapshot_id=?
                    """,
                    (tenant_id, subject_id, workbook_id, raw_snapshot_id),
                ).fetchone()
                if row is None:
                    return None
                return _stored_from_row(
                    row,
                    workbook_id=workbook_id,
                    raw_snapshot_id=raw_snapshot_id,
                )
        except WorkbookAssetRepositoryError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            raise WorkbookAssetRepositoryError("raw workbook lookup failed") from None

    def get_head_snapshot(
        self,
        *,
        principal: ServicePrincipal,
        workbook_id: str,
    ) -> StoredRawWorkbookSnapshot | None:
        tenant_id, subject_id = _principal_scope(principal)
        _workbook_id(workbook_id)
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT s.raw_snapshot_id, s.workbook_sha256, s.size_bytes, s.status,
                           s.origin_kind, s.prepared_bundle_created, s.created_at, s.asset_ref
                    FROM raw_snapshot_heads h
                    JOIN raw_snapshots s
                      ON s.tenant_id=h.tenant_id AND s.subject_id=h.subject_id
                     AND s.workbook_id=h.workbook_id AND s.raw_snapshot_id=h.raw_snapshot_id
                    WHERE h.tenant_id=? AND h.subject_id=? AND h.workbook_id=?
                    """,
                    (tenant_id, subject_id, workbook_id),
                ).fetchone()
                if row is None:
                    return None
                return _stored_from_row(
                    row,
                    workbook_id=workbook_id,
                    raw_snapshot_id=row["raw_snapshot_id"],
                )
        except WorkbookAssetRepositoryError:
            raise
        except (TypeError, ValueError, sqlite3.Error):
            raise WorkbookAssetRepositoryError("raw workbook head lookup failed") from None

    def _require_claim(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        command_sha256: str,
        command_id: str,
        claim_token: str,
        claim_fence: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT command_sha256, command_id, workbook_id, state, claim_token,
                   claim_fence, claim_expires_at
            FROM upload_commands
            WHERE tenant_id=? AND subject_id=? AND idempotency_key=?
            """,
            (tenant_id, subject_id, idempotency_key),
        ).fetchone()
        if (
            row is None
            or row["state"] != "pending"
            or row["command_sha256"] != command_sha256
            or row["command_id"] != command_id
            or row["claim_token"] != claim_token
            or row["claim_fence"] != claim_fence
            or not isinstance(row["claim_expires_at"], int)
            or row["claim_expires_at"] <= self._clock_epoch()
        ):
            raise WorkbookAssetClaimError("upload claim mismatch")
        return row

    def _before_publish_commit(
        self,
        connection: sqlite3.Connection,
        stored: StoredRawWorkbookSnapshot,
    ) -> None:
        """Test seam for proving that a mid-publication exception rolls back all catalog rows."""

    def _clock_epoch(self) -> int:
        try:
            value = self._now()
        except Exception:
            raise WorkbookAssetRepositoryError("workbook asset repository clock failed") from None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise WorkbookAssetRepositoryError("workbook asset repository clock is invalid")
        return int(value.astimezone(timezone.utc).timestamp())

    def _prepare_database_file(self) -> None:
        parent = self._database.parent
        _assert_no_symlink_components(parent)
        try:
            metadata = parent.stat(follow_symlinks=False)
        except OSError:
            raise WorkbookAssetRepositoryError(
                "workbook asset database parent is unavailable"
            ) from None
        if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink():
            raise WorkbookAssetRepositoryError("workbook asset database parent is invalid")
        if self._database.is_symlink():
            raise WorkbookAssetRepositoryError(
                "workbook asset database cannot be a symbolic link"
            )
        if self._database.exists():
            try:
                metadata = self._database.stat(follow_symlinks=False)
            except OSError:
                raise WorkbookAssetRepositoryError(
                    "workbook asset database is unavailable"
                ) from None
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkbookAssetRepositoryError(
                    "workbook asset database must be a regular file"
                )
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self._database, flags, 0o600)
            except FileExistsError:
                if self._database.is_symlink():
                    raise WorkbookAssetRepositoryError(
                        "workbook asset database cannot be a symbolic link"
                    ) from None
            except OSError:
                raise WorkbookAssetRepositoryError(
                    "workbook asset database creation failed"
                ) from None
            else:
                os.close(descriptor)
        _private_file(self._database)

    def _connect(self) -> sqlite3.Connection:
        _assert_no_symlink_components(self._database)
        if self._database.is_symlink():
            raise WorkbookAssetRepositoryError(
                "workbook asset database cannot be a symbolic link"
            )
        _private_file(self._database)
        try:
            connection = sqlite3.connect(
                self._database,
                timeout=self._timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA temp_store=MEMORY")
            return connection
        except sqlite3.Error:
            raise WorkbookAssetRepositoryError(
                "workbook asset database connection failed"
            ) from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
            self._secure_sidecars()

    def _secure_sidecars(self) -> None:
        _private_file(self._database)
        for suffix in ("-journal", "-wal", "-shm"):
            candidate = Path(str(self._database) + suffix)
            if candidate.is_symlink():
                raise WorkbookAssetRepositoryError(
                    "workbook asset database sidecar is invalid"
                )
            if candidate.exists():
                _private_file(candidate)


def _stored_from_row(
    row: sqlite3.Row,
    *,
    workbook_id: str,
    raw_snapshot_id: str,
) -> StoredRawWorkbookSnapshot:
    try:
        prepared = row["prepared_bundle_created"]
        if prepared not in (0, 1):
            raise ValueError("invalid prepared flag")
        snapshot = RawWorkbookSnapshot(
            schema_version=RAW_SNAPSHOT_SCHEMA_VERSION,
            workbook_id=workbook_id,
            raw_snapshot_id=raw_snapshot_id,
            workbook_sha256=row["workbook_sha256"],
            size_bytes=row["size_bytes"],
            status=row["status"],
            origin_kind=row["origin_kind"],
            prepared_bundle_created=bool(prepared),
            created_at=row["created_at"],
        )
        asset = StoredWorkbookAsset(
            asset_ref=row["asset_ref"],
            workbook_sha256=snapshot.workbook_sha256,
            size_bytes=snapshot.size_bytes,
        )
        return StoredRawWorkbookSnapshot(snapshot=snapshot, asset=asset)
    except Exception:
        raise WorkbookAssetRepositoryError("stored raw workbook snapshot is invalid") from None


def _encode_snapshot(snapshot: RawWorkbookSnapshot) -> str:
    if not isinstance(snapshot, RawWorkbookSnapshot):
        raise TypeError("snapshot must be a RawWorkbookSnapshot")
    return _canonical_json(snapshot.to_public_dict())


def _decode_snapshot(value: object) -> RawWorkbookSnapshot:
    if not isinstance(value, str):
        raise WorkbookAssetRepositoryError("stored upload receipt is invalid")
    try:
        document = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("invalid JSON number")
            ),
        )
        if (
            not isinstance(document, dict)
            or set(document) != _SNAPSHOT_FIELDS
            or document.get("schema_version") != RAW_SNAPSHOT_SCHEMA_VERSION
            or _canonical_json(document) != value
        ):
            raise ValueError("receipt fields are invalid")
        return RawWorkbookSnapshot(**document)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise WorkbookAssetRepositoryError("stored upload receipt is invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _principal_scope(principal: ServicePrincipal) -> tuple[str, str]:
    if not isinstance(principal, ServicePrincipal):
        raise TypeError("principal must be a ServicePrincipal")
    return principal.scope


def _idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("idempotency_key is invalid")
    return value


def _opaque(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an opaque identifier")
    return value


def _workbook_id(value: object) -> str:
    if not isinstance(value, str) or _WORKBOOK_ID_RE.fullmatch(value) is None:
        raise ValueError("workbook_id is invalid")
    return value


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sha256 identifier")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError("claim_token is invalid")
    return value


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise WorkbookAssetRepositoryError(
                "workbook asset database path is unavailable"
            ) from None
        except OSError:
            raise WorkbookAssetRepositoryError(
                "workbook asset database path is unavailable"
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkbookAssetRepositoryError(
                "workbook asset database path contains a symlink"
            )


def _private_file(path: Path) -> None:
    try:
        path.chmod(0o600, follow_symlinks=False)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, NotImplementedError):
        raise WorkbookAssetRepositoryError(
            "workbook asset database permissions are unavailable"
        ) from None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise WorkbookAssetRepositoryError("workbook asset database is not private")


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS workbooks(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        workbook_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(tenant_id, subject_id, workbook_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_snapshots(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        workbook_id TEXT NOT NULL,
        raw_snapshot_id TEXT NOT NULL,
        workbook_sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 67108864),
        status TEXT NOT NULL CHECK(status='stored'),
        origin_kind TEXT NOT NULL CHECK(origin_kind='upload'),
        prepared_bundle_created INTEGER NOT NULL CHECK(prepared_bundle_created=0),
        created_at TEXT NOT NULL,
        asset_ref TEXT NOT NULL,
        PRIMARY KEY(tenant_id, subject_id, workbook_id, raw_snapshot_id),
        FOREIGN KEY(tenant_id, subject_id, workbook_id)
          REFERENCES workbooks(tenant_id, subject_id, workbook_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_snapshot_heads(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        workbook_id TEXT NOT NULL,
        raw_snapshot_id TEXT NOT NULL,
        head_version INTEGER NOT NULL CHECK(head_version >= 1),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(tenant_id, subject_id, workbook_id),
        FOREIGN KEY(tenant_id, subject_id, workbook_id, raw_snapshot_id)
          REFERENCES raw_snapshots(tenant_id, subject_id, workbook_id, raw_snapshot_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS upload_commands(
        tenant_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        command_sha256 TEXT NOT NULL,
        command_id TEXT NOT NULL,
        workbook_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending', 'completed')),
        claim_token TEXT,
        claim_fence INTEGER NOT NULL CHECK(claim_fence >= 1),
        claim_expires_at INTEGER,
        receipt_json TEXT,
        PRIMARY KEY(tenant_id, subject_id, idempotency_key),
        CHECK(
            (state='pending' AND claim_token IS NOT NULL
             AND claim_expires_at IS NOT NULL AND receipt_json IS NULL)
            OR
            (state='completed' AND claim_token IS NULL
             AND claim_expires_at IS NULL AND receipt_json IS NOT NULL)
        )
    ) WITHOUT ROWID
    """,
)


__all__ = ["SQLiteWorkbookAssetRepository"]
