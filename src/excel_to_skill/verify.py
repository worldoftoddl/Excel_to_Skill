"""§8.1 verify — 패키지 단위 검증(V1 스키마 + V2 실재성 + V3 재현성).

- **V1 스키마**: `schemas/{meta,references,diagnostics}.schema.json`으로 세 결정론
  산출물을 검증한다(엄격: additionalProperties=false). cells.jsonl은 스키마 대상이
  아니라 각 줄 JSON 파싱 sanity만 본다. `semantics.json`은 있을 때만 조건부로
  `semantics.schema.json` 검증한다(해석 계층). 필수 파일 존재 검사에는 M2 산출물
  SKILL.md·layout/*.html도 포함한다.
- **V2 실재성(M3)**: `semantics.json`이 있으면 모든 evidence 주소와 fields 셀 주소가
  (a) 형식 유효 (b) 실존 (c) `meta.sheets[].dimensions`(D-01 used range) 범위 내인지
  검증한다(§8.1, 형식별 파서 플러그인 — 스프레드시트만 구현, docx는 M4에서 생략됨).
  approve 전 필수 통과. semantics가 없으면 이 검사 자체를 건너뛴다.
- **V3 재현성**: `--source` 원본이 주어지면 임시 폴더로 재변환해 결정론 계층을 비교한다
  (meta.json은 generated_at 제외). 대조 대상은 data 3종 + SKILL.md(고정 경로) +
  layout/*.html(목록·내용) + (있으면) defined_names_full.json. 원본이 없으면 **실패가
  아니라 생략**으로 보고하고 V1만으로 통과를 판정한다. 원본이 주어졌는데 불일치면 실패.
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
    "SKILL.md",
    "data/cells.jsonl",
    "data/references.json",
    "data/diagnostics.json",
]
# 고정 경로 결정론 산출물(V3 바이트 대조 대상 — fresh convert와 비교).
# layout/*.html은 파일명이 시트명 기반 가변이라 목록·내용을 별도 로직으로 대조한다.
# SKILL.md는 여기 넣지 않는다: 승인판은 해석 계층(semantics)에서 파생돼 fresh
# convert(항상 draft)와 바이트가 다를 수 있다. 대신 별도의 SKILL 자기일관성 검사
# (_check_skill_consistency)가 SKILL.md가 현재 패키지 파일에서 재생성한 결과와
# 일치하는지 원본 없이도 검증한다(훼손 검출 — v1.7 defect 보정).
_DETERMINISTIC = [
    "data/cells.jsonl",
    "data/references.json",
    "data/diagnostics.json",
]
_LAYOUT_DIR = "layout"
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


def _layout_htmls(pkg: Path) -> list[Path]:
    """패키지의 layout/*.html을 파일명 정렬로 돌려준다(없으면 빈 리스트)."""
    d = pkg / _LAYOUT_DIR
    return sorted(d.glob("*.html")) if d.is_dir() else []


def _check_files(pkg: Path) -> Check:
    missing = [rel for rel in _REQUIRED if not (pkg / rel).is_file()]
    # layout/은 시트별 html이라 파일명이 가변 — 디렉터리 + html 1개 이상을 본다.
    if not _layout_htmls(pkg):
        missing.append("layout/*.html")
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


def _layout_diffs(pkg: Path, fresh: Path) -> list[str]:
    """layout/*.html의 파일 목록·내용을 재변환 결과와 대조한 차이 목록."""
    want = {p.name for p in _layout_htmls(pkg)}
    got = {p.name for p in _layout_htmls(fresh)}
    diffs: list[str] = []
    if want != got:
        diffs.append(f"layout 파일 목록 불일치: {sorted(want ^ got)}")
    for name in sorted(want & got):
        if (pkg / _LAYOUT_DIR / name).read_bytes() != (fresh / _LAYOUT_DIR / name).read_bytes():
            diffs.append(f"layout/{name}")
    return diffs


def _check_skill_consistency(pkg: Path) -> Check:
    """SKILL.md가 현재 패키지 파일에서 재생성한 결과와 일치하는지(원본 불요).

    승인판/draft 모두 SKILL.md는 meta·references·diagnostics·cells·layout·semantics
    에서 결정론적으로 재생성된다. 그 재생성 결과와 바이트가 다르면 훼손이거나 구버전
    이므로 실패. V3(fresh convert)가 SKILL.md를 대조하지 않는 대신 이 검사가 담보한다.
    """
    from .emit_skill_md import build_skill_md_from_package

    try:
        expected = build_skill_md_from_package(pkg)
        actual = (pkg / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as e:
        return Check("SKILL", False, f"재생성/읽기 실패: {e}")
    if actual == expected:
        return Check("SKILL", True, "SKILL.md가 재생성 결과와 일치")
    return Check(
        "SKILL", False,
        "SKILL.md가 meta/references/diagnostics/semantics 재생성 결과와 불일치(훼손/구버전)",
    )


def _check_annotation_consistency(pkg: Path) -> Check:
    """meta.annotation ↔ semantics.review/generator 일관성(원본 불요).

    meta.annotation을 운영 필드로 쓰므로(annotate/review가 갱신), semantics와 어긋나면
    후속 캐시/승계가 잘못된 상태를 읽는다. 검사:
      - semantics 있으면: present=true · review_status==review.status ·
        annotator_version==generator.annotator_version.
      - semantics 없으면: present=false.
    둘 중 하나만 있거나 값이 어긋나면 실패.
    """
    try:
        meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return Check("annotation", False, f"meta 읽기 실패: {e}")
    ann = meta.get("annotation", {}) or {}
    sem_path = pkg / "data" / "semantics.json"
    problems: list[str] = []

    if sem_path.is_file():
        try:
            sem = json.loads(sem_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return Check("annotation", False, f"semantics 읽기 실패: {e}")
        if not ann.get("present"):
            problems.append("semantics.json이 있는데 meta.annotation.present=false")
        rs = sem.get("review", {}).get("status")
        if ann.get("review_status") != rs:
            problems.append(
                f"review_status 불일치: meta={ann.get('review_status')!r} ↔ semantics={rs!r}"
            )
        gen = sem.get("generator", {})
        av = gen.get("annotator_version")
        if ann.get("annotator_version") != av:
            problems.append(
                f"annotator_version 불일치: meta={ann.get('annotator_version')!r} ↔ semantics={av!r}"
            )
        # 완료 marker 검증: annotation_key가 non-null이면 semantics.generator+meta로
        # 재계산한 4성분 키와 일치해야 한다(훼손·가짜 완료 키 주입 검출). null이면
        # partial(미완료) draft로 허용 — 완료성은 approve 게이트가 본다.
        ak = ann.get("annotation_key")
        if ak is not None:
            from . import cache

            expected = cache.annotation_key(
                meta.get("source", {}).get("sha256", ""),
                av or "",
                gen.get("model", ""),
                gen.get("prompt_sha", ""),
            )
            if ak != expected:
                problems.append("annotation_key 불일치(훼손 또는 재계산과 다름)")
    else:
        if ann.get("present"):
            problems.append("semantics.json이 없는데 meta.annotation.present=true")
        if ann.get("annotation_key") is not None:
            problems.append("semantics.json이 없는데 meta.annotation.annotation_key가 있음")

    if problems:
        return Check("annotation", False, f"meta↔semantics 불일치: {problems}")
    return Check("annotation", True, "meta.annotation ↔ semantics 일관")


def _check_evidence(pkg: Path) -> Check:
    """V2 — semantics.json의 evidence·fields 셀 주소 실재성(§8.1).

    semantics가 있을 때만 호출된다. meta가 없거나 JSON이 깨지면 크래시가 아니라
    실패 Check로 보고한다(누락 패키지 방어). docx 등 미구현 형식은 생략으로 본다.
    """
    from .evidence import collect_evidence_problems

    meta_f = pkg / "meta.json"
    sem_f = pkg / "data/semantics.json"
    if not meta_f.is_file():
        return Check("V2", False, "meta.json 없음 — 실재성 대조 불가")
    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
        semantics = json.loads(sem_f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return Check("V2", False, f"읽기/파싱 실패: {e}")
    try:
        problems = collect_evidence_problems(semantics, meta)
    except NotImplementedError as e:
        return Check("V2", True, f"{e} — 실재성 검증 생략", skipped=True)
    if problems:
        shown = problems[:10]
        more = f" 외 {len(problems) - len(shown)}건" if len(problems) > len(shown) else ""
        return Check("V2", False, f"실재성 실패 {len(problems)}건: {shown}{more}")
    return Check("V2", True, "evidence·필드 주소 모두 실재")


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
        diffs.extend(_layout_diffs(pkg, fresh))

        def _meta_norm(p: Path) -> dict:
            d = json.loads((p / "meta.json").read_text(encoding="utf-8"))
            d.pop("generated_at", None)  # 매 변환 가변
            d.pop("annotation", None)  # 해석 계층 상태(annotate/review가 갱신) — 비결정론
            return d

        if _meta_norm(pkg) != _meta_norm(fresh):
            diffs.append("meta.json(generated_at 제외)")

    ok = not diffs
    return Check(
        "V3", ok, "재변환 결과 동일(결정론)" if ok else f"불일치: {diffs}"
    )


def verify_package(pkg: Path, source: Path | None = None) -> VerifyResult:
    """패키지를 검증한다. source가 있으면 V3(재현성)까지 수행."""
    files_check = _check_files(pkg)
    checks = [files_check]
    schema_ok: dict[str, bool] = {}
    for rel, schema_name in _SCHEMA_MAP.items():
        c = _check_schema(pkg, rel, schema_name)
        schema_ok[rel] = c.ok
        checks.append(c)
    checks.append(_check_cells_jsonl(pkg))
    checks.append(_check_full_names(pkg))

    # SKILL 자기일관성(원본 불요). 필수 파일이 빠졌으면 재생성이 크래시하므로 생략
    # (files가 이미 실패 = verify 실패). defect-3와 같은 선행-게이팅 원칙.
    if files_check.ok:
        checks.append(_check_skill_consistency(pkg))
    else:
        checks.append(
            Check("SKILL", True, "필수 파일 누락 — SKILL 일관성 검증 생략", skipped=True)
        )

    # meta.annotation ↔ semantics 일관성(원본 불요, meta만 있으면 수행 가능).
    if (pkg / "meta.json").is_file():
        checks.append(_check_annotation_consistency(pkg))

    if (pkg / "data/semantics.json").is_file():
        sem_check = _check_schema(pkg, "data/semantics.json", "semantics.schema.json")
        checks.append(sem_check)
        # V2는 스키마를 통과한 문서만 대상으로 한다. semantics 스키마가 깨졌거나
        # meta 스키마가 깨졌으면(=sheets/format 구조를 신뢰 불가) 실재성 검증은
        # 크래시 위험이 있으므로 생략으로 보고한다(선행 실패가 이미 verify 실패).
        if not sem_check.ok:
            checks.append(
                Check("V2", True, "semantics 스키마 실패 — 실재성 검증 생략", skipped=True)
            )
        elif not schema_ok.get("meta.json", False):
            checks.append(
                Check("V2", True, "meta 스키마 실패 — 실재성 검증 생략", skipped=True)
            )
        else:
            checks.append(_check_evidence(pkg))

    if source is None:
        checks.append(
            Check("V3", True, "원본 미제공 — 재현성 검증 생략(--source)", skipped=True)
        )
    elif not files_check.ok:
        # 필수 파일이 빠진 패키지는 재변환 대조 대상이 없어 read가 크래시한다.
        # files가 이미 실패(=verify 실패)이므로 V3는 생략으로 보고한다.
        checks.append(
            Check("V3", True, "필수 파일 누락 — 재현성 검증 생략", skipped=True)
        )
    else:
        checks.append(_check_reproducibility(pkg, source))

    return VerifyResult(checks)
