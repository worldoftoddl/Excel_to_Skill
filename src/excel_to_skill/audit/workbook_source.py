"""Opaque, digest-bound access to an uploaded workbook asset.

The inspection layer must not receive a filesystem path from a model request.  A web or CLI
adapter instead binds one opaque asset identifier to a reader and injects the resulting provider.
Application code still hashes the returned bytes, so a provider cannot silently substitute a
different upload for the package's ``meta.source.sha256``.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


MAX_WORKBOOK_SOURCE_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MESSAGES = {
    "INVALID_SOURCE_BINDING": "원본 workbook source binding이 유효하지 않습니다.",
    "SOURCE_UNAVAILABLE": "원본 workbook asset을 읽을 수 없습니다.",
    "SOURCE_CONTRACT_MISMATCH": "원본 workbook provider 계약이 유효하지 않습니다.",
    "SOURCE_DIGEST_MISMATCH": "원본 workbook asset이 package source digest와 일치하지 않습니다.",
    "SOURCE_LIMIT_EXCEEDED": "원본 workbook asset byte 상한을 초과했습니다.",
}


class WorkbookSourceError(RuntimeError):
    """A raw workbook asset could not be read without weakening its digest binding."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@runtime_checkable
class WorkbookSourceProvider(Protocol):
    """A provider already bound to one opaque workbook asset.

    ``expected_sha256`` is deliberately supplied by the package reader rather than by the model.
    Implementations may use it for repository-side conditional reads, but the caller will verify
    the returned bytes independently as well.
    """

    def read_bound_source(self, *, expected_sha256: str, max_bytes: int) -> bytes:
        ...


AssetReader = Callable[[str, int], bytes]


@dataclass(frozen=True, slots=True)
class BoundWorkbookSourceProvider:
    """Bind an opaque asset identifier to an injected byte reader.

    The private identifier is never included in inspection results or error messages.  The reader
    receives ``(asset_id, max_bytes)`` and may resolve that identifier through a database or object
    store.  Paths remain an implementation detail of that repository.
    """

    asset_id: str = field(repr=False)
    reader: AssetReader = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.asset_id, str)
            or not self.asset_id
            or len(self.asset_id) > 512
            or not callable(self.reader)
        ):
            raise WorkbookSourceError(
                "INVALID_SOURCE_BINDING", "원본 workbook asset binding이 유효하지 않습니다."
            )

    def read_bound_source(self, *, expected_sha256: str, max_bytes: int) -> bytes:
        expected = _sha256(expected_sha256)
        limit = _byte_limit(max_bytes)
        try:
            value = self.reader(self.asset_id, limit)
        except WorkbookSourceError as e:
            raise _sanitized(e) from None
        except Exception as e:  # noqa: BLE001 - repository boundary; never expose its path/error
            raise WorkbookSourceError(
                "SOURCE_UNAVAILABLE", "원본 workbook asset을 읽을 수 없습니다."
            ) from None
        data = _bytes(value, limit=limit)
        if hashlib.sha256(data).hexdigest() != expected:
            raise WorkbookSourceError(
                "SOURCE_DIGEST_MISMATCH",
                "원본 workbook asset이 package source digest와 일치하지 않습니다.",
            )
        return data


def _sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise WorkbookSourceError(
            "INVALID_SOURCE_BINDING", "기대 source digest가 유효하지 않습니다."
        )
    return value


def _byte_limit(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_WORKBOOK_SOURCE_BYTES
    ):
        raise WorkbookSourceError(
            "INVALID_SOURCE_BINDING", "원본 workbook byte 상한이 유효하지 않습니다."
        )
    return value


def _bytes(value: object, *, limit: int) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise WorkbookSourceError(
            "SOURCE_CONTRACT_MISMATCH", "원본 workbook provider가 bytes를 반환하지 않았습니다."
        )
    data = bytes(value)
    if len(data) > limit:
        raise WorkbookSourceError(
            "SOURCE_LIMIT_EXCEEDED", "원본 workbook asset byte 상한을 초과했습니다."
        )
    return data


def _sanitized(error: WorkbookSourceError) -> WorkbookSourceError:
    code = error.code if error.code in _SAFE_MESSAGES else "SOURCE_UNAVAILABLE"
    return WorkbookSourceError(code, _SAFE_MESSAGES[code])


def read_verified_workbook_source(
    provider: WorkbookSourceProvider | None,
    *,
    expected_sha256: str,
    max_bytes: int = MAX_WORKBOOK_SOURCE_BYTES,
) -> bytes:
    """Read and independently verify one provider-bound workbook without exposing its locator."""
    expected = _sha256(expected_sha256)
    limit = _byte_limit(max_bytes)
    if provider is None or not isinstance(provider, WorkbookSourceProvider):
        raise WorkbookSourceError(
            "SOURCE_UNAVAILABLE", "원본 workbook source provider가 연결되지 않았습니다."
        )
    try:
        value = provider.read_bound_source(expected_sha256=expected, max_bytes=limit)
    except WorkbookSourceError as e:
        raise _sanitized(e) from None
    except Exception as e:  # noqa: BLE001 - provider boundary; sanitize implementation details
        raise WorkbookSourceError(
            "SOURCE_UNAVAILABLE", "원본 workbook asset을 읽을 수 없습니다."
        ) from None
    data = _bytes(value, limit=limit)
    if hashlib.sha256(data).hexdigest() != expected:
        raise WorkbookSourceError(
            "SOURCE_DIGEST_MISMATCH",
            "원본 workbook asset이 package source digest와 일치하지 않습니다.",
        )
    return data
