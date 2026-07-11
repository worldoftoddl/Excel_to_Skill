"""§4.1 meta.json 방출기 — 패키지 표지(변환 출처 정보).

결정론 계층 파일이지만 **유일하게 가변 값(generated_at)이 허용**된다(§4.1).
그 외 모든 값은 원본 파일과 IR에서 결정론적으로 나온다.

- tool / converter_version: 도구 이름과 버전. 버전은 pyproject를 유일 출처로 삼아
  설치 메타데이터(importlib.metadata)에서 읽는다(이중 관리 방지).
- source: 원본 파일명·sha256(바이트 해시 64자)·크기·형식.
- loader_path: 어느 로더 경로로 열렸는지(§5).
- sheets: 시트별 {name, dimensions, max_row, max_col}. dimensions는 dimension
  레코드가 아니라 §5 재계산 used range다(D-01) — SheetIR.dimensions가 이미 그 값.
- generated_at: 변환 시각(ISO8601, UTC). None이면 지금 시각. 재현성 비교(V3)는
  이 필드만 제외하고 정규화 비교한다.
- annotation: 해석 계층(semantics) 상태. M1 단계는 미주석이라 전부 고정값.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .extractor import WorkbookIR

_TOOL = "excel_to_skill"
_DIST_NAME = "excel-to-skill"  # pyproject [project].name (importlib 조회 키)
_SHA_CHUNK = 1 << 20  # 1MiB


def _converter_version() -> str:
    """pyproject를 유일 출처로 삼아 설치 메타데이터에서 버전을 읽는다."""
    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:  # 미설치 환경 — 정직하게 표시
        return "0.0.0+unknown"


def _source_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_SHA_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    """ISO 8601 UTC, 초 단위 고정('...Z')."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


_DEFAULT_MAX_ROWS = 5000  # emit_html.DEFAULT_MAX_ROWS와 동기(순환 import 회피용 리터럴)


def build_meta(
    ir: WorkbookIR,
    generated_at: str | None = None,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
    full_names: bool = False,
) -> dict:
    """meta.json 문서(dict)를 만든다. 필드 순서는 §4.1 스키마 고정.

    conversion_params: 결정론 출력(layout·truncations·defined_names_full 존재)을
    좌우하는 변환 파라미터를 패키지가 자기증언하게 한다. verify --source 재변환이
    이 값을 읽어 재현한다. max_rows(layout 절단)와 full_names(전량 덤프 산출물의
    존재 자체를 바꿈)가 그 파라미터다.
    """
    path = ir.source_path
    sheets = [
        {
            "name": sh.name,
            "dimensions": sh.dimensions,  # §5 재계산 used range (D-01)
            "max_row": sh.max_row,
            "max_col": sh.max_col,
        }
        for sh in ir.sheets
    ]
    return {
        "tool": _TOOL,
        "converter_version": _converter_version(),
        "source": {
            "filename": path.name,
            "sha256": _source_sha256(path),
            "size_bytes": path.stat().st_size,
            "format": ir.format,
        },
        "loader_path": ir.loader_path,
        "conversion_params": {"max_rows": max_rows, "full_names": full_names},
        "sheets": sheets,
        "generated_at": generated_at if generated_at is not None else _now_iso(),
        "annotation": {
            "present": False,
            "annotator_version": None,
            "review_status": None,
            "annotation_key": None,  # 완료된 주석 marker(패키지-독립) — annotate가 채움
        },
    }


_KEEP = object()  # set_annotation에서 "이 필드는 기존 값 유지"를 뜻하는 sentinel


def _write_json_atomic(path: Path, doc: dict) -> None:
    """fsync한 완성본을 교체해 meta가 commit marker로 반쪽 기록되지 않게 한다."""
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(doc, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            dir_fd = os.open(path.parent, flags)
        except OSError:
            return
        try:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
        finally:
            os.close(dir_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def set_annotation(
    pkg: Path,
    *,
    present: bool,
    annotator_version: str | None,
    review_status: str | None,
    annotation_key=_KEEP,
) -> None:
    """meta.json의 annotation 블록을 갱신한다(해석 계층 상태를 provenance에 반영).

    annotate/review가 semantics를 바꿀 때 meta도 함께 맞춰, meta가 semantics 상태와
    모순되지 않게 한다. 이 블록은 비결정론(해석 계층)이므로 verify V3의 meta 비교에서는
    제외된다. 형식(indent·개행·allow_nan)은 write_meta와 동일하게 유지한다.

    `annotation_key`는 **완료된 주석 marker(패키지-독립)**다 — annotate가 완료 시 4성분
    키를, partial이면 None을 넣는다. review는 이 값을 건드리지 않으므로 생략(=_KEEP)해
    기존 값을 보존한다.
    """
    p = Path(pkg) / "meta.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    prev = doc.get("annotation", {})
    doc["annotation"] = {
        "present": present,
        "annotator_version": annotator_version,
        "review_status": review_status,
        "annotation_key": prev.get("annotation_key") if annotation_key is _KEEP else annotation_key,
    }
    _write_json_atomic(p, doc)


def set_audit_preparation(
    pkg: Path,
    *,
    status: str,
    version: str,
    facts_key: str,
    standards_key: str,
    brief_key: str,
    prepared_at: str | None = None,
    review_status: str = "draft",
) -> None:
    """Record a successfully validated audit bundle in ``meta.json``.

    This state is intentionally separate from the legacy ``annotation`` block.  Callers must
    write and validate all three audit artifacts before invoking this function, so a failed
    prepare attempt never advertises a half-built bundle as ready.
    """
    p = Path(pkg) / "meta.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["audit_preparation"] = {
        "present": True,
        "status": status,
        "version": version,
        "facts_key": facts_key,
        "standards_key": standards_key,
        "brief_key": brief_key,
        "prepared_at": prepared_at or _now_iso(),
        "review_status": review_status,
    }
    _write_json_atomic(p, doc)


def write_meta(
    ir: WorkbookIR,
    out_path: Path,
    generated_at: str | None = None,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
    full_names: bool = False,
) -> dict:
    """meta.json을 쓰고 문서를 반환한다."""
    doc = build_meta(ir, generated_at, max_rows=max_rows, full_names=full_names)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    return doc
