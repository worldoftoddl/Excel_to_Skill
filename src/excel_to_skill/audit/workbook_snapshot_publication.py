"""Provider-neutral foundations for durable workbook snapshot publication.

This module deliberately stops before source-head compare-and-swap and edit-workflow
coordination.  It defines the private server-side boundaries used to reacquire a saved
workbook, persist its exact bytes in an immutable content-addressed store, and materialize
the small public publication receipt already consumed by the Office Add-in.

Client requests never choose an asset path, provider, workbook identity, digest, or revision.
Those values come from :class:`SnapshotPublicationBasis`, the registered server-side
reacquirer, and bytes independently hashed by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

import openpyxl

from .workbook_inspection import WorkbookInspectionError, _validate_xlsx_archive


MAX_PUBLISHED_WORKBOOK_BYTES = 64 * 1024 * 1024

_OPAQUE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_MANIFEST_REF_RE = re.compile(r"\Aedit-manifest:([0-9a-f]{64})\Z")
_ASSET_REF_RE = re.compile(r"\Aworkbook-asset:([0-9a-f]{64})\Z")

_SAFE_MESSAGES = {
    "INVALID_BASIS": "The workbook snapshot publication basis is invalid.",
    "INVALID_ACQUIRED_WORKBOOK": "The reacquired workbook does not match the publication contract.",
    "INVALID_STORED_ASSET": "The stored workbook asset does not match the publication contract.",
    "SOURCE_LIMIT_EXCEEDED": "The saved workbook exceeds the publication byte limit.",
    "WORKBOOK_INSTANCE_MISMATCH": "The saved workbook instance does not match the verified execution.",
    "WORKSHEET_MISMATCH": "The saved workbook worksheet does not match the verified execution.",
    "REVISION_NOT_ADVANCED": "The saved workbook revision did not advance.",
    "REVISION_CHAIN_MISMATCH": "The saved workbook revision is not the direct transition from the pinned base.",
    "WORKBOOK_NOT_CHANGED": "The saved workbook bytes did not change.",
    "SNAPSHOT_NOT_ADVANCED": "The new workbook snapshot identity did not advance.",
    "ASSET_STORE_UNAVAILABLE": "The immutable workbook asset store is unavailable.",
    "ASSET_INTEGRITY_MISMATCH": "The immutable workbook asset failed digest validation.",
    "SAVED_WORKBOOK_MISMATCH": "The saved workbook does not contain the verified authored state.",
}


class WorkbookSnapshotPublicationError(RuntimeError):
    """A fixed, path-free failure at the snapshot-publication boundary."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _SAFE_MESSAGES else "ASSET_STORE_UNAVAILABLE"
        self.code = safe_code
        super().__init__(_SAFE_MESSAGES[safe_code])


def _fail(code: str) -> WorkbookSnapshotPublicationError:
    return WorkbookSnapshotPublicationError(code)


def _opaque(value: object, *, code: str = "INVALID_BASIS") -> str:
    if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


def _bounded_bytes(value: object) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise _fail("INVALID_ACQUIRED_WORKBOOK")
    if len(value) > MAX_PUBLISHED_WORKBOOK_BYTES:
        raise _fail("SOURCE_LIMIT_EXCEEDED")
    return value


@dataclass(frozen=True, slots=True)
class SnapshotPublicationBasis:
    """Authoritative private basis loaded from one ``session_verified`` execution."""

    bundle_id: str
    execution_id: str
    manifest_ref: str
    manifest_sha256: str
    base_snapshot_id: str
    base_workbook_sha256: str
    base_revision_id: str
    sheet: str
    worksheet_id: str
    workbook_instance_id: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            _opaque(self.bundle_id)
            _opaque(self.execution_id)
            manifest_sha256 = _sha256(self.manifest_sha256, code="INVALID_BASIS")
            manifest_match = (
                _MANIFEST_REF_RE.fullmatch(self.manifest_ref)
                if isinstance(self.manifest_ref, str)
                else None
            )
            if manifest_match is None or manifest_match.group(1) != manifest_sha256:
                raise _fail("INVALID_BASIS")
            _sha256(self.base_snapshot_id, code="INVALID_BASIS")
            _sha256(self.base_workbook_sha256, code="INVALID_BASIS")
            _opaque(self.base_revision_id)
            if not isinstance(self.sheet, str) or not self.sheet or len(self.sheet) > 31:
                raise _fail("INVALID_BASIS")
            _opaque(self.worksheet_id)
            _opaque(self.workbook_instance_id)
        except WorkbookSnapshotPublicationError:
            raise
        except Exception:
            raise _fail("INVALID_BASIS") from None


@dataclass(frozen=True, slots=True)
class AcquiredWorkbook:
    """Exact bytes plus a provider-attested direct transition from the pinned base."""

    provider_revision_id: str
    predecessor_revision_id: str = field(repr=False)
    worksheet_id: str = field(repr=False)
    workbook_instance_id: str = field(repr=False)
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        try:
            _opaque(self.provider_revision_id, code="INVALID_ACQUIRED_WORKBOOK")
            _opaque(self.predecessor_revision_id, code="INVALID_ACQUIRED_WORKBOOK")
            _opaque(self.worksheet_id, code="INVALID_ACQUIRED_WORKBOOK")
            _opaque(self.workbook_instance_id, code="INVALID_ACQUIRED_WORKBOOK")
            _bounded_bytes(self.content)
        except WorkbookSnapshotPublicationError:
            raise
        except Exception:
            raise _fail("INVALID_ACQUIRED_WORKBOOK") from None

    @property
    def workbook_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@runtime_checkable
class SavedWorkbookReacquirer(Protocol):
    """Host-injected reader already bound to one server-owned cloud workbook."""

    def reacquire_saved_workbook(
        self,
        *,
        expected_workbook_instance_id: str,
        base_revision_id: str,
        expected_sheet: str,
        expected_worksheet_id: str,
        max_bytes: int,
    ) -> AcquiredWorkbook:
        """Return bytes for the direct provider transition from ``base_revision_id``.

        The trusted adapter must use provider version history, an ETag precondition, or an
        equivalent server-side primitive. Merely reading whichever revision is currently newest
        is outside this contract. The returned ``predecessor_revision_id`` is checked again by
        application code; no provider locator crosses this boundary.
        """


@dataclass(frozen=True, slots=True)
class StoredWorkbookAsset:
    """Private content-addressed pointer; it contains no filesystem or provider locator."""

    asset_ref: str
    workbook_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        try:
            digest = _sha256(self.workbook_sha256, code="INVALID_STORED_ASSET")
            match = (
                _ASSET_REF_RE.fullmatch(self.asset_ref)
                if isinstance(self.asset_ref, str)
                else None
            )
            if match is None or match.group(1) != digest:
                raise _fail("INVALID_STORED_ASSET")
            if (
                not isinstance(self.size_bytes, int)
                or isinstance(self.size_bytes, bool)
                or not 1 <= self.size_bytes <= MAX_PUBLISHED_WORKBOOK_BYTES
            ):
                raise _fail("INVALID_STORED_ASSET")
        except WorkbookSnapshotPublicationError:
            raise
        except Exception:
            raise _fail("INVALID_STORED_ASSET") from None


@runtime_checkable
class ImmutableWorkbookAssetStore(Protocol):
    """Persist and re-read immutable workbook bytes by their independently computed digest."""

    def put_if_absent(self, data: bytes) -> StoredWorkbookAsset: ...

    def read_verified(self, asset: StoredWorkbookAsset) -> bytes: ...


def _private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode, follow_symlinks=False)
        actual = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    except (OSError, NotImplementedError):
        raise _fail("ASSET_STORE_UNAVAILABLE") from None
    if actual & 0o077:
        raise _fail("ASSET_STORE_UNAVAILABLE")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail("ASSET_STORE_UNAVAILABLE") from None
    try:
        os.fsync(descriptor)
    except OSError:
        raise _fail("ASSET_STORE_UNAVAILABLE") from None
    finally:
        os.close(descriptor)


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise _fail("ASSET_STORE_UNAVAILABLE") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise _fail("ASSET_STORE_UNAVAILABLE")


class LocalImmutableWorkbookAssetStore:
    """Private local reference store using an atomic hard-link commit.

    A completed object is never replaced. Concurrent identical writers either create the exact
    target once or validate the already-existing object. Existing corruption and symbolic links
    fail closed instead of being repaired or followed.
    """

    def __init__(self, root: Path | str) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            raise _fail("ASSET_STORE_UNAVAILABLE")
        _assert_no_symlink_components(candidate)
        try:
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
            if candidate.is_symlink() or not candidate.is_dir():
                raise _fail("ASSET_STORE_UNAVAILABLE")
            self._root = candidate.resolve(strict=True)
            _private_mode(self._root, 0o700)
            objects = self._root / "objects"
            if objects.is_symlink():
                raise _fail("ASSET_STORE_UNAVAILABLE")
            objects.mkdir(mode=0o700, exist_ok=True)
            if objects.is_symlink() or not objects.is_dir():
                raise _fail("ASSET_STORE_UNAVAILABLE")
            self._objects = objects.resolve(strict=True)
            if self._objects.parent != self._root:
                raise _fail("ASSET_STORE_UNAVAILABLE")
            _private_mode(self._objects, 0o700)
        except WorkbookSnapshotPublicationError:
            raise
        except OSError:
            raise _fail("ASSET_STORE_UNAVAILABLE") from None

    def _target(self, digest: str) -> Path:
        clean_digest = _sha256(digest, code="INVALID_STORED_ASSET")
        _assert_no_symlink_components(self._root)
        if self._objects.is_symlink() or self._objects.parent != self._root:
            raise _fail("ASSET_STORE_UNAVAILABLE")
        _private_mode(self._root, 0o700)
        _private_mode(self._objects, 0o700)
        target = self._objects / f"{clean_digest}.xlsx"
        if target.is_symlink():
            raise _fail("ASSET_STORE_UNAVAILABLE")
        return target

    def _read_digest(self, digest: str, *, expected_size: int) -> bytes:
        target = self._target(digest)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError:
            raise _fail("ASSET_STORE_UNAVAILABLE") from None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size != expected_size
                or metadata.st_size < 1
                or metadata.st_size > MAX_PUBLISHED_WORKBOOK_BYTES
            ):
                raise _fail("ASSET_INTEGRITY_MISMATCH")
            chunks: list[bytes] = []
            remaining = expected_size + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        except WorkbookSnapshotPublicationError:
            raise
        except OSError:
            raise _fail("ASSET_STORE_UNAVAILABLE") from None
        finally:
            os.close(descriptor)
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != digest:
            raise _fail("ASSET_INTEGRITY_MISMATCH")
        return data

    def put_if_absent(self, data: bytes) -> StoredWorkbookAsset:
        content = _bounded_bytes(data)
        digest = hashlib.sha256(content).hexdigest()
        asset = StoredWorkbookAsset(
            asset_ref="workbook-asset:" + digest,
            workbook_sha256=digest,
            size_bytes=len(content),
        )
        target = self._target(digest)
        if target.exists() or target.is_symlink():
            existing = self._read_digest(digest, expected_size=len(content))
            if existing != content:
                raise _fail("ASSET_INTEGRITY_MISMATCH")
            _fsync_directory(self._objects)
            return asset

        descriptor = -1
        temporary: Path | None = None
        linked = False
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=self._objects,
            )
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                descriptor = -1
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
                linked = True
            except FileExistsError:
                existing = self._read_digest(digest, expected_size=len(content))
                if existing != content:
                    raise _fail("ASSET_INTEGRITY_MISMATCH")
            if linked:
                _private_mode(target, 0o600)
                written = self._read_digest(digest, expected_size=len(content))
                if written != content:
                    raise _fail("ASSET_INTEGRITY_MISMATCH")
        except WorkbookSnapshotPublicationError:
            raise
        except OSError:
            raise _fail("ASSET_STORE_UNAVAILABLE") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        _fsync_directory(self._objects)
        return asset

    def read_verified(self, asset: StoredWorkbookAsset) -> bytes:
        if not isinstance(asset, StoredWorkbookAsset):
            raise _fail("INVALID_STORED_ASSET")
        return self._read_digest(
            asset.workbook_sha256,
            expected_size=asset.size_bytes,
        )


def validate_acquired_workbook(
    basis: SnapshotPublicationBasis,
    acquired: AcquiredWorkbook,
) -> None:
    """Validate private identity, revision advancement, size, and byte advancement."""

    if not isinstance(basis, SnapshotPublicationBasis):
        raise _fail("INVALID_BASIS")
    if not isinstance(acquired, AcquiredWorkbook):
        raise _fail("INVALID_ACQUIRED_WORKBOOK")
    _bounded_bytes(acquired.content)
    if acquired.workbook_instance_id != basis.workbook_instance_id:
        raise _fail("WORKBOOK_INSTANCE_MISMATCH")
    if acquired.worksheet_id != basis.worksheet_id:
        raise _fail("WORKSHEET_MISMATCH")
    if acquired.predecessor_revision_id != basis.base_revision_id:
        raise _fail("REVISION_CHAIN_MISMATCH")
    if acquired.provider_revision_id == basis.base_revision_id:
        raise _fail("REVISION_NOT_ADVANCED")
    if acquired.workbook_sha256 == basis.base_workbook_sha256:
        raise _fail("WORKBOOK_NOT_CHANGED")


def validate_saved_workbook_manifest(
    acquired: AcquiredWorkbook,
    manifest: Mapping[str, object],
) -> None:
    """Reopen the saved XLSX and match every approved authored cell and number format."""

    if not isinstance(acquired, AcquiredWorkbook) or not isinstance(manifest, Mapping):
        raise _fail("SAVED_WORKBOOK_MISMATCH")
    try:
        office_binding = manifest["office_binding"]
        expected_after = manifest["expected_after"]
        if not isinstance(office_binding, Mapping) or not isinstance(expected_after, list):
            raise _fail("SAVED_WORKBOOK_MISMATCH")
        sheet = office_binding["sheet"]
        worksheet_id = office_binding["worksheet_id"]
        if (
            not isinstance(sheet, str)
            or not sheet
            or not isinstance(worksheet_id, str)
            or worksheet_id != acquired.worksheet_id
            or not 1 <= len(expected_after) <= 100
        ):
            raise _fail("SAVED_WORKBOOK_MISMATCH")
        _validate_xlsx_archive(acquired.content)
        workbook = openpyxl.load_workbook(
            BytesIO(acquired.content),
            read_only=False,
            data_only=False,
            keep_links=False,
        )
        try:
            if sheet not in workbook.sheetnames:
                raise _fail("SAVED_WORKBOOK_MISMATCH")
            worksheet = workbook[sheet]
            seen: set[str] = set()
            for expected in expected_after:
                if not isinstance(expected, Mapping) or set(expected) != {
                    "cell",
                    "authored",
                    "number_format",
                }:
                    raise _fail("SAVED_WORKBOOK_MISMATCH")
                address = expected["cell"]
                authored = expected["authored"]
                number_format = expected["number_format"]
                if (
                    not isinstance(address, str)
                    or address in seen
                    or not isinstance(authored, Mapping)
                    or not isinstance(number_format, str)
                ):
                    raise _fail("SAVED_WORKBOOK_MISMATCH")
                seen.add(address)
                cell = worksheet[address]
                if cell.number_format != number_format or not _authored_matches_cell(
                    authored,
                    value=cell.value,
                    data_type=cell.data_type,
                ):
                    raise _fail("SAVED_WORKBOOK_MISMATCH")
        finally:
            workbook.close()
    except WorkbookSnapshotPublicationError:
        raise
    except (WorkbookInspectionError, OSError, ValueError, TypeError, KeyError, IndexError):
        raise _fail("SAVED_WORKBOOK_MISMATCH") from None
    except Exception:
        raise _fail("SAVED_WORKBOOK_MISMATCH") from None


def _authored_matches_cell(
    authored: Mapping[str, object],
    *,
    value: object,
    data_type: object,
) -> bool:
    kind = authored.get("kind")
    if kind == "blank":
        return set(authored) == {"kind"} and value is None
    if kind == "formula":
        return (
            set(authored) == {"kind", "formula"}
            and data_type == "f"
            and value == authored.get("formula")
        )
    if kind != "value" or set(authored) != {"kind", "value"} or data_type == "f":
        return False
    expected = authored.get("value")
    if isinstance(expected, bool):
        return isinstance(value, bool) and value is expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == expected
        )
    return isinstance(expected, str) and isinstance(value, str) and value == expected


def reacquire_saved_workbook(
    *,
    basis: SnapshotPublicationBasis,
    reacquirer: SavedWorkbookReacquirer,
) -> AcquiredWorkbook:
    """Reacquire and validate the exact direct-successor bytes without storing them."""

    if not isinstance(basis, SnapshotPublicationBasis):
        raise _fail("INVALID_BASIS")
    if not isinstance(reacquirer, SavedWorkbookReacquirer):
        raise _fail("ASSET_STORE_UNAVAILABLE")
    try:
        acquired = reacquirer.reacquire_saved_workbook(
            expected_workbook_instance_id=basis.workbook_instance_id,
            base_revision_id=basis.base_revision_id,
            expected_sheet=basis.sheet,
            expected_worksheet_id=basis.worksheet_id,
            max_bytes=MAX_PUBLISHED_WORKBOOK_BYTES,
        )
    except WorkbookSnapshotPublicationError:
        raise
    except Exception:
        raise _fail("ASSET_STORE_UNAVAILABLE") from None
    validate_acquired_workbook(basis, acquired)
    return acquired


def store_acquired_workbook(
    *,
    acquired: AcquiredWorkbook,
    assets: ImmutableWorkbookAssetStore,
) -> StoredWorkbookAsset:
    """Persist already validated bytes and verify the immutable store's exact readback."""

    if not isinstance(acquired, AcquiredWorkbook) or not isinstance(
        assets, ImmutableWorkbookAssetStore
    ):
        raise _fail("ASSET_STORE_UNAVAILABLE")
    try:
        stored = assets.put_if_absent(acquired.content)
        if not isinstance(stored, StoredWorkbookAsset):
            raise _fail("INVALID_STORED_ASSET")
        reread = assets.read_verified(stored)
    except WorkbookSnapshotPublicationError:
        raise
    except Exception:
        raise _fail("ASSET_STORE_UNAVAILABLE") from None
    if not isinstance(reread, bytes) or reread != acquired.content:
        raise _fail("ASSET_INTEGRITY_MISMATCH")
    return stored


def reacquire_and_store_workbook(
    *,
    basis: SnapshotPublicationBasis,
    reacquirer: SavedWorkbookReacquirer,
    assets: ImmutableWorkbookAssetStore,
) -> tuple[AcquiredWorkbook, StoredWorkbookAsset]:
    """Compatibility helper for callers without an intervening content validation step."""

    acquired = reacquire_saved_workbook(basis=basis, reacquirer=reacquirer)
    stored = store_acquired_workbook(acquired=acquired, assets=assets)
    return acquired, stored


def _snapshot_id(
    basis: SnapshotPublicationBasis,
    acquired: AcquiredWorkbook,
    stored: StoredWorkbookAsset,
) -> str:
    identity = {
        "schema_version": "audit_workbook_source_snapshot_identity.v1",
        "bundle_id": basis.bundle_id,
        "execution_id": basis.execution_id,
        "manifest_ref": basis.manifest_ref,
        "manifest_sha256": basis.manifest_sha256,
        "base_snapshot_id": basis.base_snapshot_id,
        "base_revision_id": basis.base_revision_id,
        "workbook_instance_sha256": hashlib.sha256(
            basis.workbook_instance_id.encode("utf-8")
        ).hexdigest(),
        "workbook_sha256": stored.workbook_sha256,
        "revision_id": acquired.provider_revision_id,
    }
    serialized = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_snapshot_publication(
    *,
    basis: SnapshotPublicationBasis,
    acquired: AcquiredWorkbook,
    stored: StoredWorkbookAsset,
) -> dict[str, object]:
    """Build the exact public ``audit_workbook_snapshot_publication.v1`` document."""

    validate_acquired_workbook(basis, acquired)
    if not isinstance(stored, StoredWorkbookAsset):
        raise _fail("INVALID_STORED_ASSET")
    if (
        stored.workbook_sha256 != acquired.workbook_sha256
        or stored.size_bytes != len(acquired.content)
    ):
        raise _fail("ASSET_INTEGRITY_MISMATCH")
    snapshot_id = _snapshot_id(basis, acquired, stored)
    if snapshot_id == basis.base_snapshot_id:
        raise _fail("SNAPSHOT_NOT_ADVANCED")
    return {
        "schema_version": "audit_workbook_snapshot_publication.v1",
        "bundle_id": basis.bundle_id,
        "execution_id": basis.execution_id,
        "manifest_ref": basis.manifest_ref,
        "manifest_sha256": basis.manifest_sha256,
        "base_snapshot_id": basis.base_snapshot_id,
        "base_revision_id": basis.base_revision_id,
        "snapshot_id": snapshot_id,
        "workbook_sha256": stored.workbook_sha256,
        "revision_id": acquired.provider_revision_id,
        "asset_persisted": True,
        "prepared_bundle_created": False,
    }


__all__ = [
    "AcquiredWorkbook",
    "ImmutableWorkbookAssetStore",
    "LocalImmutableWorkbookAssetStore",
    "MAX_PUBLISHED_WORKBOOK_BYTES",
    "SavedWorkbookReacquirer",
    "SnapshotPublicationBasis",
    "StoredWorkbookAsset",
    "WorkbookSnapshotPublicationError",
    "build_snapshot_publication",
    "reacquire_and_store_workbook",
    "reacquire_saved_workbook",
    "store_acquired_workbook",
    "validate_acquired_workbook",
    "validate_saved_workbook_manifest",
]
