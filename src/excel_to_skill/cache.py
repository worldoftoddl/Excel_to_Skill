"""§6 추출 캐시 — 결정론 계층 산출물의 재사용 판정(파일시스템 기준).

산출물 본체는 DB가 아니라 파일시스템 폴더에 둔다:

    converted/                               ← 출력 루트(--out DIR로 변경 가능)
    ├── _index.json                          ← 목록표(대장)
    └── {원본stem_slug}_{sha256 앞 12자}/     ← 원본 1개 = 폴더 1개

추출 캐시 키(§6)는 ``sha256(file) + converter_version``. hit 판정은 오너 결정에 따라
**"같은 sha256 + 같은 converter_version + 패키지 폴더 실재" 세 조건 모두 충족**일 때만
성립한다. 셋 중 하나라도 어긋나면 miss(재생성). 폴더명에는 sha 앞 12자만 박히므로,
전체 sha256은 색인 항목과 대조해 12자 접두 충돌(사실상 없음)을 방어한다.

- converter_version이 다르면 이 단계에선 그냥 miss로 보고 재생성한다. §6의
  semantics/review 자동 승계는 해석 계층(M3)이 생긴 뒤에 붙인다 — 지금은 유보.
- _index.json 항목의 ``generated_at``(최종생성시각)은 **가변값**이다. 색인은 결정론
  산출물 계약(§4) 밖의 운영 파일이므로 재현성 비교(V3) 대상이 아니다.

직렬화 관례는 다른 방출기(meta·emit_refs·emit_diag)와 동일: 레코드 필드는 고정 순서
(sort_keys 미사용), indent=2·ensure_ascii=False·allow_nan=False, 끝에 개행 1개. 단
``entries`` 묶음은 폴더명 키로 정렬해 삽입 순서와 무관하게 파일이 안정되도록 한다
(레코드 필드 순서를 뒤섞는 게 아니라 컬렉션만 정렬).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .meta import _converter_version, _now_iso, _source_sha256

_INDEX_NAME = "_index.json"
_INDEX_LOCK_NAME = ".index.lock"
_PACKAGE_LOCK_DIR = ".package_locks"
_INDEX_VERSION = 1
_SHA_PREFIX = 12  # 폴더명에 박는 sha256 접두 길이
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # 경로 위험 문자
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def slugify(name: str) -> str:
    """§4.0 slug 규칙: 공백류→``_``, 경로 위험 문자 제거, 한글 유지."""
    s = _UNSAFE.sub("", re.sub(r"\s+", "_", name.strip()))
    s = s.strip("._")
    return s or "untitled"


def package_dirname(source_path: Path | str, sha256_hex: str) -> str:
    """패키지 폴더명 = ``{원본stem_slug}_{sha256 앞 12자}``."""
    return f"{slugify(Path(source_path).stem)}_{sha256_hex[:_SHA_PREFIX]}"


def _index_path(root: Path) -> Path:
    return root / _INDEX_NAME


def _empty_index() -> dict:
    return {"index_version": _INDEX_VERSION, "entries": {}}


def _load_index_unlocked(root: Path) -> dict:
    """Read a structurally usable index; damage is a non-authoritative cache miss."""
    p = _index_path(root)
    try:
        with p.open(encoding="utf-8") as f:
            index = json.load(f)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty_index()
    if not isinstance(index, dict) or index.get("index_version") != _INDEX_VERSION:
        return _empty_index()
    entries = index.get("entries")
    if not isinstance(entries, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in entries.items()
    ):
        return _empty_index()
    return index


def load_index(root: Path) -> dict:
    """``converted/_index.json``을 읽는다. 없거나 손상됐으면 빈 색인을 돌려준다."""
    root = Path(root)
    with _index_lock(root):
        return _load_index_unlocked(root)


def _thread_lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _lock_file(file) -> None:
    if os.name == "nt":  # pragma: no cover - CI는 POSIX; 표준 라이브러리 Windows 경로
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


def _unlock_file(file) -> None:
    if os.name == "nt":  # pragma: no cover - CI는 POSIX
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _index_lock(root: Path):
    """Serialize one root's read-modify-write cycle across threads and processes."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with _thread_lock(root):
        lock_file = (root / _INDEX_LOCK_NAME).open("a+b")
        locked = False
        try:
            _lock_file(lock_file)
            locked = True
            yield
        finally:
            try:
                if locked:
                    _unlock_file(lock_file)
            finally:
                lock_file.close()


@contextmanager
def package_lock(pkg: Path | str):
    """Serialize every writer for one package using a lock outside the package.

    The lock inode lives under the converted root so a force conversion may replace the whole
    package directory without unlinking the active lock.  The resolved package path is hashed to
    keep arbitrary package names out of the lock filename while preserving one stable identity.
    """
    path = Path(pkg)
    root = path.parent
    root.mkdir(parents=True, exist_ok=True)
    lock_root = root / _PACKAGE_LOCK_DIR
    lock_root.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(
        str(path.resolve()).encode("utf-8")
    ).hexdigest()
    lock_path = lock_root / f"{identity}.lock"
    with _thread_lock(lock_path):
        lock_file = lock_path.open("a+b")
        locked = False
        try:
            _lock_file(lock_file)
            locked = True
            yield
        finally:
            try:
                if locked:
                    _unlock_file(lock_file)
            finally:
                lock_file.close()


def _ordered_index(index: dict) -> dict:
    if not isinstance(index, dict):
        raise ValueError("cache index는 JSON 객체여야 합니다.")
    entries = index.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("cache index.entries는 객체여야 합니다.")
    return {
        "index_version": index.get("index_version", _INDEX_VERSION),
        "entries": {key: entries[key] for key in sorted(entries)},
    }


def _fsync_directory(root: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(root, flags)
    except OSError:  # 일부 플랫폼은 directory fd/fsync를 지원하지 않는다.
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _save_index_unlocked(root: Path, index: dict) -> None:
    """Write a complete replacement and durably rename it over the public index."""
    root.mkdir(parents=True, exist_ok=True)
    ordered = _ordered_index(index)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{_INDEX_NAME}.", suffix=".tmp", dir=root
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(ordered, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, _index_path(root))
        _fsync_directory(root)
    finally:
        temp_path.unlink(missing_ok=True)


def save_index(root: Path, index: dict) -> None:
    """원자 교체로 ``_index.json``을 쓴다. entries는 폴더명 키로 정렬한다."""
    root = Path(root)
    with _index_lock(root):
        _save_index_unlocked(root, index)


def _build_entry(
    source_path: Path,
    sha256_hex: str,
    converter_version: str,
    conversion_params: dict | None,
    generated_at: str,
    annotation_key: str | None,
    review_status: str | None,
) -> dict:
    """§6 색인 항목 — 필드 순서 고정(원본명·sha256·경로·버전·변환파라미터·주석키·리뷰·시각).

    conversion_params(max_rows·full_names)는 결정론 산출을 좌우하므로 캐시 키의
    일부다. 같은 sha·같은 converter_version이어도 옵션이 다르면 다른 패키지이며,
    probe가 이 값을 대조해 stale hit(옛 옵션 패키지 재사용)을 막는다.
    """
    return {
        "source_filename": source_path.name,
        "sha256": sha256_hex,
        "package_path": package_dirname(source_path, sha256_hex),
        "converter_version": converter_version,
        "conversion_params": conversion_params,
        "annotation_key": annotation_key,  # convert 시 None → annotate가 갱신(§6)
        "review_status": review_status,  # convert 시 None → annotate/review가 갱신
        "generated_at": generated_at,  # 가변 — 재현성 비교(V3) 제외
    }


def _entry_from_package(root: Path, dirname: str) -> dict | None:
    """Reconstruct one base index entry from authoritative package metadata."""
    try:
        meta = json.loads((root / dirname / "meta.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    source = meta.get("source")
    annotation = meta.get("annotation")
    if not isinstance(source, dict):
        return None
    filename = source.get("filename")
    sha256_hex = source.get("sha256")
    converter_version = meta.get("converter_version")
    generated_at = meta.get("generated_at")
    if not all(
        isinstance(value, str) and value
        for value in (filename, sha256_hex, converter_version, generated_at)
    ):
        return None
    conversion_params = meta.get("conversion_params")
    if conversion_params is not None and not isinstance(conversion_params, dict):
        return None
    return {
        "source_filename": filename,
        "sha256": sha256_hex,
        "package_path": dirname,
        "converter_version": converter_version,
        "conversion_params": conversion_params,
        "annotation_key": (
            annotation.get("annotation_key") if isinstance(annotation, dict) else None
        ),
        "review_status": (
            annotation.get("review_status") if isinstance(annotation, dict) else None
        ),
        "generated_at": generated_at,
    }


@dataclass(frozen=True)
class CacheProbe:
    """추출 캐시 조회 결과.

    reason: hit이면 ``match``. miss면 ``force``/``absent``/``version_changed``/
    ``params_changed``/``sha_mismatch``/``folder_missing`` 중 하나 — cli가 stderr/로그로
    사유를 밝힐 때 쓴다.
    """

    hit: bool
    reason: str
    sha256: str
    package_dir: str  # 폴더명
    package_path: Path  # root / 폴더명
    entry: dict | None  # 색인에 있던 기존 항목(없으면 None)


def probe(
    root: Path,
    source_path: Path | str,
    *,
    converter_version: str | None = None,
    conversion_params: dict | None = None,
    force: bool = False,
) -> CacheProbe:
    """원본을 캐시에 대조한다. hit이면 재생성 없이 기존 폴더를 재사용하면 된다.

    conversion_params(max_rows·full_names)를 주면 색인 항목의 값과 대조해, 다르면
    ``params_changed`` miss로 재생성한다(옵션이 바뀌면 산출이 달라지므로). 주지 않으면
    이 대조를 건너뛴다(하위호환 — cli는 항상 현재 옵션을 넘긴다).
    """
    src = Path(source_path)
    cv = converter_version if converter_version is not None else _converter_version()
    sha = _source_sha256(src)
    dirname = package_dirname(src, sha)
    pkg = root / dirname
    entry = load_index(root)["entries"].get(dirname)

    if force:
        return CacheProbe(False, "force", sha, dirname, pkg, entry)
    if entry is None:
        return CacheProbe(False, "absent", sha, dirname, pkg, None)
    if entry.get("converter_version") != cv:
        return CacheProbe(False, "version_changed", sha, dirname, pkg, entry)
    if conversion_params is not None and entry.get("conversion_params") != conversion_params:
        return CacheProbe(False, "params_changed", sha, dirname, pkg, entry)
    if entry.get("sha256") != sha:  # 12자 접두 충돌 방어
        return CacheProbe(False, "sha_mismatch", sha, dirname, pkg, entry)
    if not pkg.is_dir():
        return CacheProbe(False, "folder_missing", sha, dirname, pkg, entry)
    return CacheProbe(True, "match", sha, dirname, pkg, entry)


def annotation_key(
    file_sha256: str, annotator_version: str, model: str, prompt_sha: str
) -> str:
    """주석 캐시 키(§6) = sha256(file + annotator_version + model + prompt_sha) hex.

    파일·어노테이터·모델·프롬프트 중 하나라도 바뀌면 키가 달라져 재주석 대상이 된다.
    성분 원문은 meta.source.sha256과 semantics.generator에 남으므로 키는 해시로 압축한다.
    """
    h = hashlib.sha256()
    h.update("\n".join([file_sha256, annotator_version, model, prompt_sha]).encode("utf-8"))
    return h.hexdigest()


def artifact_key(kind: str, inputs: dict) -> str:
    """Return a deterministic key for one non-deterministic audit artifact stage.

    Audit preparation has three independently invalidated layers (facts, standards, brief), so
    the legacy four-component annotation key is intentionally not reused.  Callers pass an
    explicit dependency object; canonical JSON makes semantically identical key ordering stable.
    """
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("artifact kind는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(inputs, dict):
        raise ValueError("artifact inputs는 JSON 객체여야 합니다.")
    payload = json.dumps(
        {"kind": kind.strip(), "inputs": inputs},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path | str) -> str:
    """Hash one stage input file without loading it all into memory."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_stage_recipe_key(
    stage: str,
    *,
    prepare_version: str,
    stage_version: str,
    inputs: dict,
) -> str:
    """Return a pre-execution cache key for one audit preparation stage.

    Artifact keys identify an already-produced document.  Recipe keys instead identify everything
    that can affect a stage *before* paying for that stage, which allows facts/context reuse when a
    downstream recipe changes.
    """
    for name, value in (
        ("stage", stage),
        ("prepare_version", prepare_version),
        ("stage_version", stage_version),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name}는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(inputs, dict):
        raise ValueError("audit stage inputs는 JSON 객체여야 합니다.")
    return artifact_key(
        f"audit_{stage}_recipe",
        {
            "prepare_version": prepare_version.strip(),
            "stage_version": stage_version.strip(),
            "inputs": inputs,
        },
    )


_UNSET = object()  # "이 인자는 건드리지 않음"을 명시적 None(=값을 None으로 설정)과 구분


def update_annotation(
    root: Path,
    dirname: str,
    *,
    annotation_key=_UNSET,
    review_status=_UNSET,
) -> dict | None:
    """기존 색인 항목의 해석 계층 필드만 갱신한다(결정론 필드는 불변).

    annotate/review가 semantics를 바꿀 때 `_index.json`을 맞춘다. 항목이 없거나 색인이
    손상됐으면 package meta에서 기본 항목을 복구하고, meta도 쓸 수 없을 때만 None을
    반환한다. **인자를 주면(=명시적으로 None이라도) 덮어쓰고, 생략하면 건드리지
    않는다** — annotation_key=None을 넘겨 '완료되지 않은 주석'의 키를 clear할 수 있게
    하기 위함(partial 실패 캐시 오염 방지).
    """
    root = Path(root)
    with _index_lock(root):
        index = _load_index_unlocked(root)
        entry = index.get("entries", {}).get(dirname)
        if entry is None:
            entry = _entry_from_package(root, dirname)
            if entry is not None:
                index.setdefault("entries", {})[dirname] = entry
        if entry is not None:
            if annotation_key is not _UNSET:
                entry["annotation_key"] = annotation_key
            if review_status is not _UNSET:
                entry["review_status"] = review_status
        # Also replaces a malformed index with a valid empty one when the requested entry
        # cannot be recovered.  The package files remain the source of truth.
        _save_index_unlocked(root, index)
        return entry


def update_audit(
    root: Path,
    dirname: str,
    *,
    facts_key=_UNSET,
    standards_key=_UNSET,
    brief_key=_UNSET,
    facts_recipe_key=_UNSET,
    standards_recipe_key=_UNSET,
    brief_recipe_key=_UNSET,
    prepare_version=_UNSET,
    status=_UNSET,
) -> dict | None:
    """Mirror audit keys/status, rebuilding the package's base entry when necessary."""
    root = Path(root)
    with _index_lock(root):
        index = _load_index_unlocked(root)
        entry = index.get("entries", {}).get(dirname)
        if entry is None:
            entry = _entry_from_package(root, dirname)
            if entry is not None:
                index.setdefault("entries", {})[dirname] = entry
        if entry is not None:
            audit = entry.setdefault("audit", {})
            for name, value in (
                ("facts_key", facts_key),
                ("standards_key", standards_key),
                ("brief_key", brief_key),
                ("facts_recipe_key", facts_recipe_key),
                ("standards_recipe_key", standards_recipe_key),
                ("brief_recipe_key", brief_recipe_key),
                ("prepare_version", prepare_version),
                ("status", status),
            ):
                if value is not _UNSET:
                    audit[name] = value
        _save_index_unlocked(root, index)
        return entry


def get_audit(root: Path, dirname: str) -> dict | None:
    """Return a detached audit cache record for one converted package, if present."""
    entry = load_index(root).get("entries", {}).get(dirname)
    if not isinstance(entry, dict) or not isinstance(entry.get("audit"), dict):
        return None
    # JSON round-trip prevents callers from mutating the loaded index through this result.
    return json.loads(json.dumps(entry["audit"], ensure_ascii=False))


def update_audit_scope(
    root: Path,
    dirname: str,
    scope_id: str,
    **values,
) -> dict | None:
    """Mirror one namespaced audit scope without mutating the workbook audit cache."""
    if not isinstance(scope_id, str) or not re.fullmatch(r"[0-9a-f]{64}", scope_id):
        raise ValueError("scope_id는 64자리 소문자 SHA-256이어야 합니다.")
    allowed = {
        "facts_key", "standards_key", "brief_key",
        "facts_recipe_key", "standards_recipe_key", "brief_recipe_key",
        "prepare_version", "status",
    }
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"알 수 없는 audit scope cache 필드: {unknown}")
    root = Path(root)
    with _index_lock(root):
        index = _load_index_unlocked(root)
        entry = index.get("entries", {}).get(dirname)
        if entry is None:
            entry = _entry_from_package(root, dirname)
            if entry is not None:
                index.setdefault("entries", {})[dirname] = entry
        if entry is not None:
            scoped = entry.setdefault("audit_scopes", {}).setdefault(scope_id, {})
            for name, value in values.items():
                scoped[name] = value
        _save_index_unlocked(root, index)
        return entry


def get_audit_scope(root: Path, dirname: str, scope_id: str) -> dict | None:
    """Return a detached cache record for one sheet audit scope."""
    entry = load_index(root).get("entries", {}).get(dirname)
    if not isinstance(entry, dict):
        return None
    scopes = entry.get("audit_scopes")
    if not isinstance(scopes, dict) or not isinstance(scopes.get(scope_id), dict):
        return None
    return json.loads(json.dumps(scopes[scope_id], ensure_ascii=False))


def record(
    root: Path,
    source_path: Path | str,
    *,
    sha256: str | None = None,
    converter_version: str | None = None,
    conversion_params: dict | None = None,
    generated_at: str | None = None,
    annotation_key: str | None = None,
    review_status: str | None = None,
) -> dict:
    """변환 성공 후 색인에 항목을 upsert하고, 그 항목을 반환한다.

    sha256/converter_version은 ``probe``에서 이미 계산했으면 넘겨 재해시를 피한다.
    conversion_params는 이 변환에 실제로 쓴 옵션(max_rows·full_names)을 그대로 기록한다.
    """
    src = Path(source_path)
    sha = sha256 if sha256 is not None else _source_sha256(src)
    cv = converter_version if converter_version is not None else _converter_version()
    ts = generated_at if generated_at is not None else _now_iso()
    entry = _build_entry(src, sha, cv, conversion_params, ts, annotation_key, review_status)

    root = Path(root)
    with _index_lock(root):
        index = _load_index_unlocked(root)
        index.setdefault("entries", {})[entry["package_path"]] = entry
        _save_index_unlocked(root, index)
    return entry
