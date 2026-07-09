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
) -> dict:
    """meta.json 문서(dict)를 만든다. 필드 순서는 §4.1 스키마 고정.

    conversion_params: 결정론 출력(layout·truncations)을 좌우하는 변환 파라미터를
    패키지가 자기증언하게 한다. verify --source 재변환이 이 값을 읽어 재현한다.
    후속 --full-names 등도 이 객체에 필드로 추가한다(구조를 객체로 열어둠).
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
        "conversion_params": {"max_rows": max_rows},
        "sheets": sheets,
        "generated_at": generated_at if generated_at is not None else _now_iso(),
        "annotation": {
            "present": False,
            "annotator_version": None,
            "review_status": None,
        },
    }


def write_meta(
    ir: WorkbookIR,
    out_path: Path,
    generated_at: str | None = None,
    *,
    max_rows: int = _DEFAULT_MAX_ROWS,
) -> dict:
    """meta.json을 쓰고 문서를 반환한다."""
    doc = build_meta(ir, generated_at, max_rows=max_rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    return doc
