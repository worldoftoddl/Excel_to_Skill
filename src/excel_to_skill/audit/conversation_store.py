"""Private content-addressed storage for audit conversation artifacts.

Conversation graph checkpoints must contain only small, typed references.  The
question, observations, and rendered answer live in this store instead.  Every
object is bound to a hashed thread directory and a canonical JSON envelope so a
caller can validate both ownership and content before hydrating it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TypedDict

from .model import AuditModelError, canonical_json


class ConversationArtifactStoreError(RuntimeError):
    """A conversation artifact or its reference failed the private-store contract."""


class ArtifactRef(TypedDict):
    """JSON-only pointer safe to place in a graph checkpoint."""

    kind: str
    schema_version: str
    storage_key: str
    content_sha256: str


_REF_FIELDS = frozenset(ArtifactRef.__required_keys__)
_ENVELOPE_FIELDS = frozenset({"kind", "schema_version", "payload"})
_HEX_SHA256 = r"[0-9a-f]{64}"
_STORAGE_KEY_RE = re.compile(
    rf"\Athreads/(?P<thread>{_HEX_SHA256})/objects/(?P<digest>{_HEX_SHA256})\.json\Z"
)
_DIGEST_RE = re.compile(rf"\A{_HEX_SHA256}\Z")
_MAX_THREAD_ID_LENGTH = 256
_MAX_LABEL_LENGTH = 256


def _private_chmod(path: Path, mode: int) -> None:
    """Apply private permissions when the platform/filesystem permits it."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _require_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConversationArtifactStoreError(
            f"{field}는 앞뒤 공백 없는 비어 있지 않은 문자열이어야 합니다."
        )
    if len(value) > maximum:
        raise ConversationArtifactStoreError(
            f"{field}는 {maximum}자를 초과할 수 없습니다."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConversationArtifactStoreError(f"{field}에는 제어 문자를 쓸 수 없습니다.")
    return value


def _thread_digest(thread_id: object) -> str:
    text = _require_text(
        thread_id,
        field="thread_id",
        maximum=_MAX_THREAD_ID_LENGTH,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_envelope(
    *,
    kind: object,
    schema_version: object,
    payload: object,
) -> tuple[dict[str, object], str, str]:
    normalized_kind = _require_text(
        kind,
        field="kind",
        maximum=_MAX_LABEL_LENGTH,
    )
    normalized_schema = _require_text(
        schema_version,
        field="schema_version",
        maximum=_MAX_LABEL_LENGTH,
    )
    envelope = {
        "kind": normalized_kind,
        "schema_version": normalized_schema,
        "payload": payload,
    }
    try:
        serialized = canonical_json(envelope)
    except AuditModelError as exc:
        raise ConversationArtifactStoreError(
            "conversation payload는 유한한 JSON 값으로 표현할 수 있어야 합니다."
        ) from exc
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return json.loads(serialized), serialized, digest


def _validated_ref(ref: object) -> ArtifactRef:
    if not isinstance(ref, Mapping):
        raise ConversationArtifactStoreError("artifact ref는 JSON 객체여야 합니다.")
    if set(ref) != _REF_FIELDS:
        raise ConversationArtifactStoreError(
            "artifact ref 필드가 계약과 일치하지 않습니다."
        )
    kind = _require_text(ref.get("kind"), field="ref.kind", maximum=_MAX_LABEL_LENGTH)
    schema_version = _require_text(
        ref.get("schema_version"),
        field="ref.schema_version",
        maximum=_MAX_LABEL_LENGTH,
    )
    storage_key = _require_text(
        ref.get("storage_key"),
        field="ref.storage_key",
        maximum=256,
    )
    content_sha256 = _require_text(
        ref.get("content_sha256"),
        field="ref.content_sha256",
        maximum=64,
    )
    if _STORAGE_KEY_RE.fullmatch(storage_key) is None:
        raise ConversationArtifactStoreError("ref.storage_key 형식이 유효하지 않습니다.")
    if _DIGEST_RE.fullmatch(content_sha256) is None:
        raise ConversationArtifactStoreError("ref.content_sha256 형식이 유효하지 않습니다.")
    return {
        "kind": kind,
        "schema_version": schema_version,
        "storage_key": storage_key,
        "content_sha256": content_sha256,
    }


class ConversationArtifactStore:
    """Content-addressed JSON store scoped to hashed conversation threads."""

    def __init__(self, root: Path | str) -> None:
        candidate = Path(root).expanduser()
        if candidate.is_symlink():
            raise ConversationArtifactStoreError(
                "artifact store root는 symbolic link일 수 없습니다."
            )
        if candidate.exists() and not candidate.is_dir():
            raise ConversationArtifactStoreError("artifact store root는 디렉터리여야 합니다.")
        try:
            self.root = candidate.resolve(strict=False)
            self._ensure_directory(self.root)
        except OSError as exc:
            raise ConversationArtifactStoreError(
                "artifact store root를 준비할 수 없습니다."
            ) from exc

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        if path.is_symlink():
            raise ConversationArtifactStoreError(
                "artifact store 내부 디렉터리는 symbolic link일 수 없습니다."
            )
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ConversationArtifactStoreError(
                "artifact store 디렉터리를 준비할 수 없습니다."
            ) from exc
        if not path.is_dir():
            raise ConversationArtifactStoreError(
                "artifact store 경로가 디렉터리가 아닙니다."
            )
        _private_chmod(path, 0o700)

    def _thread_objects(self, thread_digest: str) -> Path:
        threads = self.root / "threads"
        thread = threads / thread_digest
        objects = thread / "objects"
        for directory in (threads, thread, objects):
            self._ensure_directory(directory)
        return objects

    def _path_for_key(self, storage_key: str) -> Path:
        match = _STORAGE_KEY_RE.fullmatch(storage_key)
        if match is None:
            raise ConversationArtifactStoreError("storage key 형식이 유효하지 않습니다.")
        relative = PurePosixPath(storage_key)
        candidate = self.root.joinpath(*relative.parts)
        current = self.root
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                raise ConversationArtifactStoreError(
                    "artifact store 내부 경로는 symbolic link일 수 없습니다."
                )
        if candidate.is_symlink():
            raise ConversationArtifactStoreError(
                "conversation artifact는 symbolic link일 수 없습니다."
            )
        try:
            candidate.resolve(strict=False).relative_to(self.root)
        except ValueError as exc:
            raise ConversationArtifactStoreError(
                "artifact storage key가 store root를 벗어납니다."
            ) from exc
        return candidate

    def write(
        self,
        thread_id: str,
        *,
        kind: str,
        schema_version: str,
        payload: object,
    ) -> ArtifactRef:
        """Atomically persist one canonical envelope and return a refs-only pointer."""
        thread_digest = _thread_digest(thread_id)
        envelope, serialized, content_digest = _canonical_envelope(
            kind=kind,
            schema_version=schema_version,
            payload=payload,
        )
        objects = self._thread_objects(thread_digest)
        storage_key = (
            f"threads/{thread_digest}/objects/{content_digest}.json"
        )
        ref: ArtifactRef = {
            "kind": str(envelope["kind"]),
            "schema_version": str(envelope["schema_version"]),
            "storage_key": storage_key,
            "content_sha256": content_digest,
        }
        target = self._path_for_key(storage_key)
        if target.exists() or target.is_symlink():
            self.load(thread_id, ref)
            return ref

        fd = -1
        temp_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{content_digest}.",
                suffix=".tmp",
                dir=objects,
            )
            temp_path = Path(temp_name)
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                fd = -1
                file.write(serialized)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, target)
            _private_chmod(target, 0o600)
            _fsync_directory(objects)
        except (OSError, UnicodeError) as exc:
            raise ConversationArtifactStoreError(
                "conversation artifact를 원자적으로 저장할 수 없습니다."
            ) from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return ref

    def load(
        self,
        thread_id: str,
        ref: Mapping[str, object],
        *,
        expected_kind: str | None = None,
        expected_schema_version: str | None = None,
    ) -> object:
        """Load an object only after ownership, envelope, and digest validation."""
        thread_digest = _thread_digest(thread_id)
        normalized = _validated_ref(ref)
        match = _STORAGE_KEY_RE.fullmatch(normalized["storage_key"])
        assert match is not None
        if match.group("thread") != thread_digest:
            raise ConversationArtifactStoreError(
                "artifact ref가 요청한 conversation thread에 속하지 않습니다."
            )
        if match.group("digest") != normalized["content_sha256"]:
            raise ConversationArtifactStoreError(
                "artifact ref의 storage key와 content digest가 일치하지 않습니다."
            )
        if expected_kind is not None:
            required_kind = _require_text(
                expected_kind,
                field="expected_kind",
                maximum=_MAX_LABEL_LENGTH,
            )
            if normalized["kind"] != required_kind:
                raise ConversationArtifactStoreError(
                    "artifact ref kind가 기대한 값과 일치하지 않습니다."
                )
        if expected_schema_version is not None:
            required_schema = _require_text(
                expected_schema_version,
                field="expected_schema_version",
                maximum=_MAX_LABEL_LENGTH,
            )
            if normalized["schema_version"] != required_schema:
                raise ConversationArtifactStoreError(
                    "artifact ref schema_version이 기대한 값과 일치하지 않습니다."
                )

        target = self._path_for_key(normalized["storage_key"])
        try:
            descriptor = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise ConversationArtifactStoreError(
                "conversation artifact를 찾거나 읽을 수 없습니다."
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ConversationArtifactStoreError(
                    "conversation artifact는 일반 파일이어야 합니다."
                )
            with os.fdopen(descriptor, "rb") as file:
                descriptor = -1
                raw = file.read()
            text = raw.decode("utf-8")
            document = json.loads(text)
        except ConversationArtifactStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConversationArtifactStoreError(
                "conversation artifact가 유효한 UTF-8 JSON이 아닙니다."
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(document, dict) or set(document) != _ENVELOPE_FIELDS:
            raise ConversationArtifactStoreError(
                "conversation artifact envelope 필드가 계약과 일치하지 않습니다."
            )
        try:
            normalized_envelope, serialized, digest = _canonical_envelope(
                kind=document.get("kind"),
                schema_version=document.get("schema_version"),
                payload=document.get("payload"),
            )
        except ConversationArtifactStoreError as exc:
            raise ConversationArtifactStoreError(
                "conversation artifact envelope가 유효하지 않습니다."
            ) from exc
        if raw != (serialized + "\n").encode("utf-8"):
            raise ConversationArtifactStoreError(
                "conversation artifact가 canonical JSON 형식이 아닙니다."
            )
        if normalized_envelope["kind"] != normalized["kind"]:
            raise ConversationArtifactStoreError(
                "conversation artifact kind가 ref와 일치하지 않습니다."
            )
        if normalized_envelope["schema_version"] != normalized["schema_version"]:
            raise ConversationArtifactStoreError(
                "conversation artifact schema_version이 ref와 일치하지 않습니다."
            )
        if digest != normalized["content_sha256"]:
            raise ConversationArtifactStoreError(
                "conversation artifact content digest가 ref와 일치하지 않습니다."
            )
        return normalized_envelope["payload"]


__all__ = [
    "ArtifactRef",
    "ConversationArtifactStore",
    "ConversationArtifactStoreError",
]
