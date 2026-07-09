"""§8.1 verify — 패키지 단위 검증(M1: V1 스키마 + V3 재현성).

- **V1 스키마**: `schemas/{meta,references,diagnostics}.schema.json`으로 세 결정론
  산출물을 검증한다(엄격: additionalProperties=false). cells.jsonl은 스키마 대상이
  아니라 각 줄 JSON 파싱 sanity만 본다. semantics는 M3(스키마 미작성)라 있으면 생략.
- **V3 재현성**: `--source` 원본이 주어지면 임시 폴더로 재변환해 결정론 계층을 비교한다
  (meta.json은 generated_at 제외). 원본이 없으면 **실패가 아니라 생략**으로 보고하고
  V1만으로 통과를 판정한다. 원본이 주어졌는데 불일치면 verify 실패.

V2(evidence 실재성)는 해석 계층(M3)이 붙은 뒤 추가한다.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import jsonschema

# 이 파일: src/excel_to_skill/verify.py → parents[2] = 리포지토리 루트
_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

# (패키지 상대경로 → 스키마 파일)
_SCHEMA_MAP = {
    "meta.json": "meta.schema.json",
    "data/references.json": "references.schema.json",
    "data/diagnostics.json": "diagnostics.schema.json",
}
_REQUIRED = [
    "meta.json",
    "data/cells.jsonl",
    "data/references.json",
    "data/diagnostics.json",
]
_DETERMINISTIC = [
    "data/cells.jsonl",
    "data/references.json",
    "data/diagnostics.json",
]
# --full-names 시에만 존재하는 결정론 산출물. 있으면 V3 대조·V1 스키마에 포함한다.
_FULL_NAMES_REL = "data/defined_names_full.json"


@dataclass
class Check:
    """검사 하나의 결과. skipped=True면 통과/실패 판정에서 제외한다."""

    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class VerifyResult:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if not c.skipped)


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _check_files(pkg: Path) -> Check:
    missing = [rel for rel in _REQUIRED if not (pkg / rel).is_file()]
    return Check(
        "files",
        not missing,
        "필수 파일 모두 존재" if not missing else f"누락: {missing}",
    )


def _check_schema(pkg: Path, rel: str, schema_name: str) -> Check:
    f = pkg / rel
    if not f.is_file():
        return Check(f"V1:{rel}", False, "파일 없음")
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return Check(f"V1:{rel}", False, f"JSON 파싱 실패: {e}")
    try:
        jsonschema.validate(doc, _load_schema(schema_name))
    except jsonschema.ValidationError as e:
        loc = "/".join(str(p) for p in e.absolute_path) or "(root)"
        return Check(f"V1:{rel}", False, f"{loc}: {e.message}")
    return Check(f"V1:{rel}", True, "스키마 통과")


def _check_cells_jsonl(pkg: Path) -> Check:
    f = pkg / "data/cells.jsonl"
    if not f.is_file():
        return Check("cells.jsonl", False, "파일 없음")
    n = 0
    with f.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                return Check("cells.jsonl", False, f"{i}행 JSON 파싱 실패: {e}")
            if not isinstance(obj, dict):
                return Check("cells.jsonl", False, f"{i}행: 객체가 아님")
            n += 1
    return Check("cells.jsonl", True, f"{n}줄 JSON 정상")


def _check_full_names(pkg: Path) -> Check:
    """defined_names_full.json 존재 ↔ diagnostics.full_dump_present 일치 + 스키마.

    - --full-names 안 켠 패키지는 파일이 없어야 정상, full_dump_present=false.
    - 켠 패키지는 파일이 있고 full_dump_present=true.
    둘이 어긋나면(한쪽만) 실패. 파일이 있으면 스키마까지 검증한다.
    """
    f = pkg / _FULL_NAMES_REL
    present = f.is_file()
    try:
        diag = json.loads((pkg / "data/diagnostics.json").read_text(encoding="utf-8"))
        flag = diag.get("defined_names", {}).get("full_dump_present", False)
    except (OSError, json.JSONDecodeError) as e:
        return Check("full_names", False, f"diagnostics 읽기 실패: {e}")

    if present != flag:
        return Check(
            "full_names",
            False,
            f"불일치: 파일 존재={present} ↔ full_dump_present={flag}",
        )
    if not present:
        return Check("full_names", True, "미방출 — full_dump_present=false 일치")
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        jsonschema.validate(doc, _load_schema("defined_names_full.schema.json"))
    except json.JSONDecodeError as e:
        return Check("full_names", False, f"JSON 파싱 실패: {e}")
    except jsonschema.ValidationError as e:
        loc = "/".join(str(p) for p in e.absolute_path) or "(root)"
        return Check("full_names", False, f"{loc}: {e.message}")
    return Check("full_names", True, "방출 — full_dump_present=true 일치·스키마 통과")


def _check_reproducibility(pkg: Path, source: Path) -> Check:
    """원본을 임시 폴더로 재변환해 결정론 계층을 대조한다."""
    from .cli import _convert_one  # 순환 회피 위해 지연 import
    from .emit_html import DEFAULT_MAX_ROWS
    from .meta import _converter_version, _source_sha256

    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    if _source_sha256(source) != meta["source"]["sha256"]:
        return Check("V3", False, "--source가 이 패키지의 원본과 다름(sha256 불일치)")

    # 재변환 입력 = (원본 + 변환 파라미터). max_rows는 layout·truncations를,
    # full_names는 defined_names_full.json 존재를 좌우하므로 CLI 기본값이 아니라
    # 패키지가 증언한 값으로 재현해야 한다.
    params = meta.get("conversion_params", {})
    max_rows = params.get("max_rows") or DEFAULT_MAX_ROWS
    full_names = bool(params.get("full_names", False))
    # 있으면 전량 덤프도 결정론 대조 대상에 포함(양쪽 다 같은 조건으로 재변환됨).
    deterministic = list(_DETERMINISTIC)
    if (pkg / _FULL_NAMES_REL).is_file():
        deterministic.append(_FULL_NAMES_REL)
    with tempfile.TemporaryDirectory() as td:
        fresh = _convert_one(
            source,
            Path(td),
            force=True,
            cv=_converter_version(),
            max_rows=max_rows,
            full_names=full_names,
        )
        diffs = [
            rel
            for rel in deterministic
            if (pkg / rel).read_bytes() != (fresh / rel).read_bytes()
        ]

        def _meta_norm(p: Path) -> dict:
            d = json.loads((p / "meta.json").read_text(encoding="utf-8"))
            d.pop("generated_at", None)
            return d

        if _meta_norm(pkg) != _meta_norm(fresh):
            diffs.append("meta.json(generated_at 제외)")

    ok = not diffs
    return Check(
        "V3", ok, "재변환 결과 동일(결정론)" if ok else f"불일치: {diffs}"
    )


def verify_package(pkg: Path, source: Path | None = None) -> VerifyResult:
    """패키지를 검증한다. source가 있으면 V3(재현성)까지 수행."""
    checks = [_check_files(pkg)]
    for rel, schema_name in _SCHEMA_MAP.items():
        checks.append(_check_schema(pkg, rel, schema_name))
    checks.append(_check_cells_jsonl(pkg))
    checks.append(_check_full_names(pkg))

    if (pkg / "data/semantics.json").is_file():
        checks.append(
            Check("V1:semantics", True, "스키마 미작성(M3) — 생략", skipped=True)
        )

    if source is None:
        checks.append(
            Check("V3", True, "원본 미제공 — 재현성 검증 생략(--source)", skipped=True)
        )
    else:
        checks.append(_check_reproducibility(pkg, source))

    return VerifyResult(checks)
