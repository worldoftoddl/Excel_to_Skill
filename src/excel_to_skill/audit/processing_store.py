"""Immutable local storage for fully verified prepared bundle directories.

The processing pipeline builds a package in private staging, verifies it, and only then hands
the directory to this store.  Publication captures regular files into a content manifest, copies
them into a private temporary directory, and atomically renames the complete object into place.
Public receipts contain only content identifiers and bounded counts; filesystem locators remain
inside this implementation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MAX_PREPARED_BUNDLE_FILES = 20_000
MAX_PREPARED_BUNDLE_BYTES = 512 * 1024 * 1024

_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_IDENTITY_BYTES = 64 * 1024
_MAX_IDENTITY_DEPTH = 16
_MAX_IDENTITY_NODES = 20_000
_MAX_RELATIVE_PATH_BYTES = 2048
_MAX_RELATIVE_PATH_DEPTH = 64
_MAX_PACKAGE_DIRECTORIES = 20_000
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MANIFEST_NAME = "manifest.json"
_PACKAGE_NAME = "package"
_OBJECTS_NAME = "objects"
_LOCK_NAME = ".publish.lock"
_PACKAGE_MANIFEST_VERSION = "audit_prepared_package_manifest.v1"
_STORE_MANIFEST_VERSION = "audit_prepared_bundle_store_manifest.v1"
_SNAPSHOT_IDENTITY_VERSION = "audit_prepared_bundle_snapshot_identity.v1"
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_MESSAGES = {
    "INVALID_ROOT": "The prepared bundle store root is invalid.",
    "STORE_UNAVAILABLE": "The prepared bundle store is unavailable.",
    "INVALID_PACKAGE": "The prepared bundle package is invalid.",
    "PACKAGE_LIMIT_EXCEEDED": "The prepared bundle package exceeds the storage limit.",
    "PACKAGE_CHANGED": "The prepared bundle package changed during publication.",
    "INVALID_IDENTITY": "The prepared bundle identity is invalid.",
    "INVALID_STORED_BUNDLE": "The stored prepared bundle descriptor is invalid.",
    "BUNDLE_INTEGRITY_MISMATCH": "The stored prepared bundle failed integrity verification.",
}


class ProcessingStoreError(RuntimeError):
    """A fixed, path-free failure at the prepared bundle storage boundary."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _MESSAGES else "STORE_UNAVAILABLE"
        self.code = safe_code
        super().__init__(_MESSAGES[safe_code])


def _fail(code: str) -> ProcessingStoreError:
    return ProcessingStoreError(code)


def _sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _fail(code)
    return value


@dataclass(frozen=True, slots=True)
class StoredPreparedBundle:
    """Content-only receipt for one immutable prepared bundle.

    No path, object-store key, or other locator is serializable from this descriptor.
    """

    snapshot_id: str
    package_manifest_sha256: str
    file_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        try:
            _sha256(self.snapshot_id, code="INVALID_STORED_BUNDLE")
            _sha256(self.package_manifest_sha256, code="INVALID_STORED_BUNDLE")
            if (
                not isinstance(self.file_count, int)
                or isinstance(self.file_count, bool)
                or not 1 <= self.file_count <= MAX_PREPARED_BUNDLE_FILES
                or not isinstance(self.total_bytes, int)
                or isinstance(self.total_bytes, bool)
                or not 1 <= self.total_bytes <= MAX_PREPARED_BUNDLE_BYTES
            ):
                raise _fail("INVALID_STORED_BUNDLE")
        except ProcessingStoreError:
            raise
        except Exception:
            raise _fail("INVALID_STORED_BUNDLE") from None


@dataclass(frozen=True, slots=True)
class _SourceFile:
    relative_path: str
    path: Path
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalized_identity(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _fail("INVALID_IDENTITY")
    nodes = 0

    def normalize(item: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_IDENTITY_NODES or depth > _MAX_IDENTITY_DEPTH:
            raise _fail("INVALID_IDENTITY")
        if item is None or isinstance(item, (str, bool)):
            return item
        if isinstance(item, int) and not isinstance(item, bool):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _fail("INVALID_IDENTITY")
            return item
        if isinstance(item, Mapping):
            output: dict[str, object] = {}
            for key, nested in item.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or any(ord(character) < 0x20 for character in key)
                ):
                    raise _fail("INVALID_IDENTITY")
                output[key] = normalize(nested, depth + 1)
            return output
        if isinstance(item, (list, tuple)):
            return [normalize(nested, depth + 1) for nested in item]
        raise _fail("INVALID_IDENTITY")

    try:
        normalized = normalize(value, 0)
        if not isinstance(normalized, dict):
            raise _fail("INVALID_IDENTITY")
        encoded = _canonical_json(normalized)
    except ProcessingStoreError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _fail("INVALID_IDENTITY") from None
    if len(encoded) > _MAX_IDENTITY_BYTES:
        raise _fail("INVALID_IDENTITY")
    return normalized


def _assert_no_symlink_components(path: Path, *, code: str) -> None:
    if not path.is_absolute():
        raise _fail(code)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise _fail(code) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise _fail(code)


def _private_mode(path: Path, mode: int, *, code: str) -> None:
    try:
        path.chmod(mode, follow_symlinks=False)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, NotImplementedError):
        raise _fail(code) from None
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise _fail(code)


def _fsync_directory(path: Path, *, code: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise _fail(code) from None


def _relative_path(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise _fail(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise _fail(code) from None
    path = PurePosixPath(value)
    if (
        not value
        or len(encoded) > _MAX_RELATIVE_PATH_BYTES
        or path.is_absolute()
        or value != path.as_posix()
        or len(path.parts) > _MAX_RELATIVE_PATH_DEPTH
        or any(
            part in {"", ".", ".."}
            or "\\" in part
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
            for part in path.parts
        )
    ):
        raise _fail(code)
    return value


def _scan_package(
    package: Path,
    *,
    max_files: int,
    max_total_bytes: int,
    require_private: bool,
    error_code: str,
) -> list[_SourceFile]:
    limit_code = (
        "BUNDLE_INTEGRITY_MISMATCH"
        if error_code == "BUNDLE_INTEGRITY_MISMATCH"
        else "PACKAGE_LIMIT_EXCEEDED"
    )
    try:
        root_stat = package.lstat()
    except OSError:
        raise _fail(error_code) from None
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise _fail(error_code)
    if require_private and stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise _fail(error_code)

    found: list[_SourceFile] = []
    total = 0
    directory_count = 1
    stack: list[tuple[Path, tuple[str, ...]]] = [(package, ())]
    while stack:
        directory, relative_parts = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            raise _fail(error_code) from None
        for entry in entries:
            relative = "/".join((*relative_parts, entry.name))
            _relative_path(relative, code=error_code)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise _fail(error_code) from None
            mode = metadata.st_mode
            if stat.S_ISLNK(mode):
                raise _fail(error_code)
            if stat.S_ISDIR(mode):
                if require_private and stat.S_IMODE(mode) & 0o077:
                    raise _fail(error_code)
                directory_count += 1
                if directory_count > _MAX_PACKAGE_DIRECTORIES:
                    raise _fail(limit_code)
                stack.append((Path(entry.path), (*relative_parts, entry.name)))
                continue
            if not stat.S_ISREG(mode):
                raise _fail(error_code)
            if require_private and stat.S_IMODE(mode) & 0o077:
                raise _fail(error_code)
            if metadata.st_size < 0:
                raise _fail(error_code)
            total += metadata.st_size
            if len(found) + 1 > max_files or total > max_total_bytes:
                raise _fail(limit_code)
            found.append(
                _SourceFile(
                    relative_path=relative,
                    path=Path(entry.path),
                    size_bytes=metadata.st_size,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    mtime_ns=metadata.st_mtime_ns,
                    ctime_ns=metadata.st_ctime_ns,
                )
            )
    if not found:
        raise _fail(error_code)
    return sorted(found, key=lambda item: item.relative_path)


def _open_source(
    source: _SourceFile,
    *,
    code: str = "PACKAGE_CHANGED",
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(source.path, flags)
        metadata = os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise _fail(code) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != source.device
        or metadata.st_ino != source.inode
        or metadata.st_size != source.size_bytes
        or metadata.st_mtime_ns != source.mtime_ns
        or metadata.st_ctime_ns != source.ctime_ns
    ):
        os.close(descriptor)
        raise _fail(code)
    return descriptor, metadata


def _copy_and_hash(
    source: _SourceFile,
    target: Path,
    *,
    remaining_bytes: int,
) -> dict[str, object]:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_mode(target.parent, 0o700, code="STORE_UNAVAILABLE")
    source_fd, before = _open_source(source)
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    target_fd = -1
    digest = hashlib.sha256()
    observed = 0
    try:
        target_fd = os.open(target, target_flags, 0o600)
        os.fchmod(target_fd, 0o600)
        while True:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            observed += len(chunk)
            if observed > source.size_bytes or observed > remaining_bytes:
                raise _fail("PACKAGE_CHANGED")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        after = os.fstat(source_fd)
        if (
            observed != source.size_bytes
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise _fail("PACKAGE_CHANGED")
        os.fsync(target_fd)
    except ProcessingStoreError:
        raise
    except OSError:
        raise _fail("STORE_UNAVAILABLE") from None
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)
    return {
        "path": source.relative_path,
        "size_bytes": observed,
        "sha256": digest.hexdigest(),
    }


def _write_manifest(path: Path, document: dict[str, object]) -> None:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise _fail("PACKAGE_LIMIT_EXCEEDED")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        raise _fail("STORE_UNAVAILABLE") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_manifest(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or not 1 <= metadata.st_size <= _MAX_MANIFEST_BYTES
        ):
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        chunks: list[bytes] = []
        remaining = _MAX_MANIFEST_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(_COPY_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != metadata.st_size or len(data) > _MAX_MANIFEST_BYTES:
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        document = json.loads(data.decode("utf-8"))
    except ProcessingStoreError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise _fail("BUNDLE_INTEGRITY_MISMATCH") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(document, dict):
        raise _fail("BUNDLE_INTEGRITY_MISMATCH")
    return document


def _package_manifest(files: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": _PACKAGE_MANIFEST_VERSION, "files": files}


def _package_manifest_sha256(files: list[dict[str, object]]) -> str:
    return hashlib.sha256(_canonical_json(_package_manifest(files))).hexdigest()


def _snapshot_id(identity_sha256: str, package_manifest_sha256: str) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "schema_version": _SNAPSHOT_IDENTITY_VERSION,
                "identity_sha256": identity_sha256,
                "package_manifest_sha256": package_manifest_sha256,
            }
        )
    ).hexdigest()


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _publication_lock(path: Path):
    with _process_lock(path):
        descriptor = -1
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise _fail("STORE_UNAVAILABLE")
            file = os.fdopen(descriptor, "a+b")
            descriptor = -1
        except ProcessingStoreError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            raise _fail("STORE_UNAVAILABLE") from None
        locked = False
        try:
            if os.name == "nt":  # pragma: no cover - CI and production reference host are POSIX
                import msvcrt

                file.seek(0, os.SEEK_END)
                if file.tell() == 0:
                    file.write(b"\0")
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            try:
                if locked:
                    if os.name == "nt":  # pragma: no cover
                        import msvcrt

                        file.seek(0)
                        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            finally:
                file.close()


class LocalPreparedBundleStore:
    """Private content-addressed directory store for verified prepared packages."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_files: int = MAX_PREPARED_BUNDLE_FILES,
        max_total_bytes: int = MAX_PREPARED_BUNDLE_BYTES,
    ) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            raise _fail("INVALID_ROOT")
        if (
            not isinstance(max_files, int)
            or isinstance(max_files, bool)
            or not 1 <= max_files <= MAX_PREPARED_BUNDLE_FILES
            or not isinstance(max_total_bytes, int)
            or isinstance(max_total_bytes, bool)
            or not 1 <= max_total_bytes <= MAX_PREPARED_BUNDLE_BYTES
        ):
            raise ValueError("prepared bundle store limits are invalid")
        _assert_no_symlink_components(candidate, code="INVALID_ROOT")
        try:
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
            if candidate.is_symlink() or not candidate.is_dir():
                raise _fail("INVALID_ROOT")
            self._root = candidate.resolve(strict=True)
            _private_mode(self._root, 0o700, code="INVALID_ROOT")
            objects = self._root / _OBJECTS_NAME
            if objects.is_symlink():
                raise _fail("INVALID_ROOT")
            objects.mkdir(exist_ok=True, mode=0o700)
            if objects.is_symlink() or not objects.is_dir():
                raise _fail("INVALID_ROOT")
            self._objects = objects.resolve(strict=True)
            if self._objects.parent != self._root:
                raise _fail("INVALID_ROOT")
            _private_mode(self._objects, 0o700, code="INVALID_ROOT")
        except ProcessingStoreError:
            raise
        except OSError:
            raise _fail("INVALID_ROOT") from None
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        self._lock_path = self._root / _LOCK_NAME
        self._deployment_id = hashlib.sha256(
            _canonical_json(
                {
                    "schema_version": "audit_prepared_bundle_store_deployment.v1",
                    "root": str(self._root),
                }
            )
        ).hexdigest()

    @property
    def deployment_id(self) -> str:
        """Opaque host-local namespace used to reject restart against another store root."""

        return self._deployment_id

    def publish(
        self,
        package: Path,
        identity: Mapping[str, object],
    ) -> StoredPreparedBundle:
        """Capture and atomically publish one exact package directory."""

        source = Path(package)
        if not source.is_absolute():
            raise _fail("INVALID_PACKAGE")
        _assert_no_symlink_components(source, code="INVALID_PACKAGE")
        try:
            source = source.resolve(strict=True)
        except OSError:
            raise _fail("INVALID_PACKAGE") from None
        if (
            source == self._root
            or self._root in source.parents
            or source in self._root.parents
        ):
            raise _fail("INVALID_PACKAGE")
        normalized_identity = _normalized_identity(identity)
        identity_sha256 = hashlib.sha256(_canonical_json(normalized_identity)).hexdigest()
        initial = _scan_package(
            source,
            max_files=self._max_files,
            max_total_bytes=self._max_total_bytes,
            require_private=False,
            error_code="INVALID_PACKAGE",
        )

        temporary = Path(tempfile.mkdtemp(prefix=".bundle.", dir=self._objects))
        published = False
        try:
            _private_mode(temporary, 0o700, code="STORE_UNAVAILABLE")
            copied_package = temporary / _PACKAGE_NAME
            copied_package.mkdir(mode=0o700)
            _private_mode(copied_package, 0o700, code="STORE_UNAVAILABLE")
            files: list[dict[str, object]] = []
            total_bytes = 0
            for source_file in initial:
                record = _copy_and_hash(
                    source_file,
                    copied_package / PurePosixPath(source_file.relative_path),
                    remaining_bytes=self._max_total_bytes - total_bytes,
                )
                total_bytes += int(record["size_bytes"])
                files.append(record)
            if total_bytes < 1:
                raise _fail("INVALID_PACKAGE")
            current = _scan_package(
                source,
                max_files=self._max_files,
                max_total_bytes=self._max_total_bytes,
                require_private=False,
                error_code="PACKAGE_CHANGED",
            )
            current_identity = [
                (
                    item.relative_path,
                    item.size_bytes,
                    item.device,
                    item.inode,
                    item.mtime_ns,
                    item.ctime_ns,
                )
                for item in current
            ]
            initial_identity = [
                (
                    item.relative_path,
                    item.size_bytes,
                    item.device,
                    item.inode,
                    item.mtime_ns,
                    item.ctime_ns,
                )
                for item in initial
            ]
            if current_identity != initial_identity:
                raise _fail("PACKAGE_CHANGED")

            manifest_sha256 = _package_manifest_sha256(files)
            snapshot_id = _snapshot_id(identity_sha256, manifest_sha256)
            stored = StoredPreparedBundle(
                snapshot_id=snapshot_id,
                package_manifest_sha256=manifest_sha256,
                file_count=len(files),
                total_bytes=total_bytes,
            )
            manifest = {
                "schema_version": _STORE_MANIFEST_VERSION,
                "snapshot_id": snapshot_id,
                "identity_sha256": identity_sha256,
                "package_manifest_sha256": manifest_sha256,
                "file_count": len(files),
                "total_bytes": total_bytes,
                "files": files,
            }
            _write_manifest(temporary / _MANIFEST_NAME, manifest)
            copied_directories = sorted(
                (path for path in copied_package.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in copied_directories:
                _private_mode(directory, 0o700, code="STORE_UNAVAILABLE")
                _fsync_directory(directory, code="STORE_UNAVAILABLE")
            _fsync_directory(copied_package, code="STORE_UNAVAILABLE")
            _fsync_directory(temporary, code="STORE_UNAVAILABLE")

            target = self._target(snapshot_id)
            with _publication_lock(self._lock_path):
                if target.exists() or target.is_symlink():
                    self._resolve(stored)
                    return stored
                try:
                    temporary.rename(target)
                    published = True
                except OSError:
                    raise _fail("STORE_UNAVAILABLE") from None
                _fsync_directory(self._objects, code="STORE_UNAVAILABLE")
                self._resolve(stored)
                return stored
        finally:
            if not published:
                shutil.rmtree(temporary, ignore_errors=True)

    def resolve(self, stored: StoredPreparedBundle) -> Path:
        """Return the private package directory only after complete readback verification."""

        if not isinstance(stored, StoredPreparedBundle):
            raise _fail("INVALID_STORED_BUNDLE")
        with _publication_lock(self._lock_path):
            return self._resolve(stored)

    def _target(self, snapshot_id: str) -> Path:
        digest = _sha256(snapshot_id, code="INVALID_STORED_BUNDLE")
        _assert_no_symlink_components(self._root, code="STORE_UNAVAILABLE")
        if self._objects.is_symlink() or self._objects.parent != self._root:
            raise _fail("STORE_UNAVAILABLE")
        _private_mode(self._root, 0o700, code="STORE_UNAVAILABLE")
        _private_mode(self._objects, 0o700, code="STORE_UNAVAILABLE")
        target = self._objects / digest
        if target.is_symlink():
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        return target

    def _resolve(self, stored: StoredPreparedBundle) -> Path:
        target = self._target(stored.snapshot_id)
        try:
            metadata = target.lstat()
        except OSError:
            raise _fail("STORE_UNAVAILABLE") from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        try:
            with os.scandir(target) as iterator:
                entries = {entry.name: entry for entry in iterator}
        except OSError:
            raise _fail("STORE_UNAVAILABLE") from None
        if set(entries) != {_MANIFEST_NAME, _PACKAGE_NAME}:
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        package = target / _PACKAGE_NAME
        manifest = _read_manifest(target / _MANIFEST_NAME)
        expected_keys = {
            "schema_version",
            "snapshot_id",
            "identity_sha256",
            "package_manifest_sha256",
            "file_count",
            "total_bytes",
            "files",
        }
        if set(manifest) != expected_keys or manifest.get("schema_version") != _STORE_MANIFEST_VERSION:
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        identity_sha256 = _sha256(
            manifest.get("identity_sha256"), code="BUNDLE_INTEGRITY_MISMATCH"
        )
        package_manifest_sha256 = _sha256(
            manifest.get("package_manifest_sha256"),
            code="BUNDLE_INTEGRITY_MISMATCH",
        )
        files = manifest.get("files")
        if not isinstance(files, list):
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        normalized_files: list[dict[str, object]] = []
        previous_path: str | None = None
        total_bytes = 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
                raise _fail("BUNDLE_INTEGRITY_MISMATCH")
            relative = _relative_path(item.get("path"), code="BUNDLE_INTEGRITY_MISMATCH")
            size_bytes = item.get("size_bytes")
            digest = _sha256(item.get("sha256"), code="BUNDLE_INTEGRITY_MISMATCH")
            if (
                not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or (previous_path is not None and relative <= previous_path)
            ):
                raise _fail("BUNDLE_INTEGRITY_MISMATCH")
            total_bytes += size_bytes
            if len(normalized_files) + 1 > self._max_files or total_bytes > self._max_total_bytes:
                raise _fail("BUNDLE_INTEGRITY_MISMATCH")
            normalized_files.append(
                {"path": relative, "size_bytes": size_bytes, "sha256": digest}
            )
            previous_path = relative
        if (
            not normalized_files
            or manifest.get("snapshot_id") != stored.snapshot_id
            or manifest.get("file_count") != stored.file_count
            or manifest.get("total_bytes") != stored.total_bytes
            or len(normalized_files) != stored.file_count
            or total_bytes != stored.total_bytes
            or package_manifest_sha256 != stored.package_manifest_sha256
            or _package_manifest_sha256(normalized_files) != package_manifest_sha256
            or _snapshot_id(identity_sha256, package_manifest_sha256) != stored.snapshot_id
        ):
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")

        observed = _scan_package(
            package,
            max_files=self._max_files,
            max_total_bytes=self._max_total_bytes,
            require_private=True,
            error_code="BUNDLE_INTEGRITY_MISMATCH",
        )
        if [item.relative_path for item in observed] != [
            str(item["path"]) for item in normalized_files
        ]:
            raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        for source_file, expected in zip(observed, normalized_files, strict=True):
            if source_file.size_bytes != expected["size_bytes"]:
                raise _fail("BUNDLE_INTEGRITY_MISMATCH")
            digest = hashlib.sha256()
            descriptor, _metadata = _open_source(
                source_file,
                code="BUNDLE_INTEGRITY_MISMATCH",
            )
            observed_bytes = 0
            try:
                while True:
                    chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    observed_bytes += len(chunk)
                    digest.update(chunk)
                after = os.fstat(descriptor)
            except OSError:
                raise _fail("BUNDLE_INTEGRITY_MISMATCH") from None
            finally:
                os.close(descriptor)
            if (
                observed_bytes != expected["size_bytes"]
                or after.st_dev != source_file.device
                or after.st_ino != source_file.inode
                or after.st_size != source_file.size_bytes
                or after.st_mtime_ns != source_file.mtime_ns
                or after.st_ctime_ns != source_file.ctime_ns
                or digest.hexdigest() != expected["sha256"]
            ):
                raise _fail("BUNDLE_INTEGRITY_MISMATCH")
        return package


__all__ = [
    "LocalPreparedBundleStore",
    "MAX_PREPARED_BUNDLE_BYTES",
    "MAX_PREPARED_BUNDLE_FILES",
    "ProcessingStoreError",
    "StoredPreparedBundle",
]
