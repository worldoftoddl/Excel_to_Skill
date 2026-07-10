"""M3 2단계: 어노테이터 테스트(§7) — 클라이언트 주입으로 LLM 없이 결정론 고정.

실 anthropic 호출·API 키·패키지 설치 없이, 정해진 응답을 돌려주는 스텁 클라이언트로
happy path / 스키마불일치 1회 재시도 / 단위 제외 / P1 경계 / 무키 방어를 못박는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from excel_to_skill import annotator
from excel_to_skill.annotator import annotate_package
from excel_to_skill.cli import _convert_one
from excel_to_skill.meta import _converter_version
from excel_to_skill.verify import verify_package

FX_DIR = Path(__file__).parent / "fixtures"

# fx1: 시트 Data, used range A1:B5 — 이 범위 안 주소만 V2 통과.
_SHEET_OK = json.dumps({
    "name": "Data", "purpose": "표 한 장", "evidence": ["Data!A1"], "confidence": 0.9,
    "sections": [{
        "range": "A1:B5", "semantic_type": "table_header",
        "evidence": ["Data!A1:B1"], "confidence": 0.8,
        "fields": [{"label_cell": "A1", "value_cell": "B1", "role": "머리"}],
    }],
}, ensure_ascii=False)
_WB_OK = json.dumps({
    "workbook_claims": [{"claim": "표 하나", "evidence": ["Data!A1:B5"], "confidence": 0.9}]
}, ensure_ascii=False)
_BAD = "이건 JSON이 아닙니다"
# 스키마는 통과하지만 evidence가 used range(A1:B5) 밖 — V2 실재성 실패용.
_SHEET_BAD_ADDR = json.dumps({
    "name": "Data", "purpose": "표", "evidence": ["Data!ZZ999"], "confidence": 0.5,
}, ensure_ascii=False)
_WB_BAD_ADDR = json.dumps({
    "workbook_claims": [{"claim": "x", "evidence": ["Data!ZZ999"], "confidence": 0.5}]
}, ensure_ascii=False)


class StubClient:
    """호출 순서대로 미리 정한 응답을 돌려주는 가짜 클라이언트."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, *, system: str, user: str, schema: dict | None = None) -> str:
        self.calls.append(user)
        return self.responses.pop(0)


def _pkg(tmp_path: Path) -> Path:
    return _convert_one(
        FX_DIR / "fx1_merge_formula.xlsx", tmp_path, force=True, cv=_converter_version()
    )


def _prompt_sha() -> str:
    p = Path(annotator.__file__).resolve().parents[2] / "prompts" / "annotator_v1.md"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_annotate_happy_path_and_v2(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    stub = StubClient([_SHEET_OK, _WB_OK])
    res = annotate_package(pkg, client=stub)

    assert res["sheets"] == 1 and res["excluded"] == []
    assert len(stub.calls) == 2  # 시트 1 + 워크북 1
    sem = json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))
    # generator 계약
    g = sem["generator"]
    assert g["model"] == annotator.DEFAULT_MODEL
    assert g["annotator_version"] == annotator.ANNOTATOR_VERSION
    assert g["prompt_sha"] == _prompt_sha() and g["temperature"] == 0
    assert g["generated_at"]
    # review는 생성 시 draft
    assert sem["review"] == {"status": "draft", "reviewed_at": None, "note": None}
    assert sem["sheets"][0]["name"] == "Data"
    # 생성 직후 verify V2(실재성) 통과
    v2 = next(c for c in verify_package(pkg).checks if c.name == "V2")
    assert v2.ok and not v2.skipped, v2.detail


def test_annotate_model_override(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    annotate_package(pkg, model="custom-model", client=StubClient([_SHEET_OK, _WB_OK]))
    sem = json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))
    assert sem["generator"]["model"] == "custom-model"


def test_annotate_retries_once_on_schema_mismatch(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    # 시트 첫 응답 불량 → 오류 첨부 재시도 → 정상. 단위 제외 없이 포함돼야.
    stub = StubClient([_BAD, _SHEET_OK, _WB_OK])
    res = annotate_package(pkg, client=stub)
    assert res["sheets"] == 1 and res["excluded"] == []
    assert len(stub.calls) == 3  # bad, retry(good), wb
    assert "[재시도]" in stub.calls[1]  # 재시도 메시지에 오류 첨부


def test_annotate_excludes_unit_after_second_failure(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path)
    # 시트 2연속 실패 → 제외. 워크북은 별개로 진행.
    stub = StubClient([_BAD, _BAD, _WB_OK])
    res = annotate_package(pkg, client=stub)
    assert res["sheets"] == 0 and res["excluded"] == ["Data"]
    sem = json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))
    assert sem["sheets"] == []  # 제외돼 비어 있음
    assert len(sem["workbook_claims"]) == 1  # 워크북은 성공
    # 빈 sheets여도 스키마·V2 성립(sheets는 선택, workbook_claims 주소는 실재)
    checks = verify_package(pkg).checks
    assert next(c for c in checks if c.name == "V1:data/semantics.json").ok
    assert next(c for c in checks if c.name == "V2").ok


def test_annotate_retries_then_excludes_on_v2_invalid_evidence(tmp_path: Path) -> None:
    """스키마 통과여도 evidence가 used range 밖이면 재시도→재실패 시 단위 제외."""
    pkg = _pkg(tmp_path)
    # 시트: V2-불량 2연속 → 제외. 워크북: V2-불량 2연속 → 제외(빈 배열).
    stub = StubClient([_SHEET_BAD_ADDR, _SHEET_BAD_ADDR, _WB_BAD_ADDR, _WB_BAD_ADDR])
    res = annotate_package(pkg, client=stub)
    assert res["sheets"] == 0 and res["excluded"] == ["Data", "workbook_claims"]
    assert "used range" in stub.calls[1]  # 재시도 메시지에 실재성 사유 첨부
    sem = json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))
    assert sem["sheets"] == [] and sem["workbook_claims"] == []
    # 핵심 계약: 산출물은 V2를 통과해야 한다(불량 evidence가 남지 않음).
    v2 = next(c for c in verify_package(pkg).checks if c.name == "V2")
    assert v2.ok, v2.detail


def test_annotate_recovers_v2_invalid_on_retry(tmp_path: Path) -> None:
    """V2 실패 후 재시도에서 실재 주소로 고치면 단위가 포함된다."""
    pkg = _pkg(tmp_path)
    stub = StubClient([_SHEET_BAD_ADDR, _SHEET_OK, _WB_OK])
    res = annotate_package(pkg, client=stub)
    assert res["sheets"] == 1 and res["excluded"] == []
    assert len(stub.calls) == 3
    v2 = next(c for c in verify_package(pkg).checks if c.name == "V2")
    assert v2.ok, v2.detail


def test_annotate_cache_hit_skips_llm(tmp_path: Path) -> None:
    """같은 키 재실행은 재주석 생략(클라이언트 미호출) — 캐시 hit."""
    from excel_to_skill import cache

    pkg = _pkg(tmp_path)
    root, dirname = pkg.parent, pkg.name
    r1 = annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    assert r1.get("cached") is False
    # 색인에 annotation_key·review_status 기록됨
    entry = cache.load_index(root)["entries"][dirname]
    assert entry["annotation_key"] and entry["review_status"] == "draft"

    # 두 번째 호출: 응답 없는 스텁을 줘도 캐시 hit면 호출되지 않아야(pop 안 됨)
    empty = StubClient([])
    r2 = annotate_package(pkg, client=empty)
    assert r2.get("cached") is True and r2["sheets"] == 1
    assert empty.responses == []  # 애초에 소비 대상 없음(호출 자체가 없었음 확인용)


def test_annotate_force_reannotates(tmp_path: Path) -> None:
    """--force면 캐시 hit여도 재주석(LLM 재호출)."""
    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    stub = StubClient([_SHEET_OK, _WB_OK])
    r = annotate_package(pkg, client=stub, force=True)
    assert r.get("cached") is False
    assert stub.responses == []  # 재주석하며 2개 응답 모두 소비


def test_annotate_cache_miss_on_model_change(tmp_path: Path) -> None:
    """모델이 바뀌면 annotation_key가 달라져 재주석(캐시 miss)."""
    pkg = _pkg(tmp_path)
    annotate_package(pkg, model="m1", client=StubClient([_SHEET_OK, _WB_OK]))
    stub = StubClient([_SHEET_OK, _WB_OK])
    r = annotate_package(pkg, model="m2", client=stub)  # 다른 모델
    assert r.get("cached") is False and stub.responses == []


def test_partial_annotate_does_not_poison_cache(tmp_path: Path) -> None:
    """부분 실패(excluded 있음)는 annotation_key를 남기지 않아 다음 실행이 재시도한다."""
    from excel_to_skill import cache

    pkg = _pkg(tmp_path)
    root, dirname = pkg.parent, pkg.name
    # 1차: 시트 2연속 실패 → excluded=['Data'], key 미기록
    r1 = annotate_package(pkg, client=StubClient([_BAD, _BAD, _WB_OK]))
    assert r1["excluded"] == ["Data"] and r1["sheets"] == 0
    assert cache.load_index(root)["entries"][dirname]["annotation_key"] is None

    # 2차: 좋은 stub → 캐시 hit이 아니라 재시도(호출됨)해서 성공
    stub = StubClient([_SHEET_OK, _WB_OK])
    r2 = annotate_package(pkg, client=stub)
    assert r2.get("cached") is False and r2["sheets"] == 1 and stub.responses == []
    assert cache.load_index(root)["entries"][dirname]["annotation_key"]  # 이제 기록됨


def test_force_partial_clears_existing_key(tmp_path: Path) -> None:
    """기존 완료 키가 있어도 --force 재주석이 부분 실패하면 키를 clear한다."""
    from excel_to_skill import cache

    pkg = _pkg(tmp_path)
    root, dirname = pkg.parent, pkg.name
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))  # 완료 → 키 기록
    assert cache.load_index(root)["entries"][dirname]["annotation_key"]

    # --force인데 부분 실패 → 기존 키 clear(다음 실행이 stale hit 안 나게)
    annotate_package(pkg, client=StubClient([_BAD, _BAD, _WB_OK]), force=True)
    assert cache.load_index(root)["entries"][dirname]["annotation_key"] is None


def test_cache_hit_requires_marker_and_generator_agreement(tmp_path: Path) -> None:
    """캐시 hit는 _index만이 아니라 패키지 marker·generator 재계산까지 일치해야 한다.

    generator를 훼손하면(=verify가 실패시키는 상태) _index/meta 키는 그대로여도 재계산
    키가 어긋나므로, 캐시 hit이 아니라 재주석해서 정상 복구되어야 한다(approve·승계와
    같은 완료 기준)."""
    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))  # 완료
    sem_path = pkg / "data/semantics.json"
    sem = json.loads(sem_path.read_text(encoding="utf-8"))
    sem["generator"]["model"] = "tampered-model"  # 재계산 키만 어긋남
    sem_path.write_text(json.dumps(sem, ensure_ascii=False, indent=2), encoding="utf-8")
    ann = next(c for c in verify_package(pkg).checks if c.name == "annotation")
    assert not ann.ok  # verify는 이미 실패 상태

    stub = StubClient([_SHEET_OK, _WB_OK])  # no-force → hit이면 소비 안 됨
    r = annotate_package(pkg, client=stub)
    assert r.get("cached") is False and stub.responses == []  # 재주석함(hit 아님)
    assert next(c for c in verify_package(pkg).checks if c.name == "annotation").ok


def test_cache_miss_when_package_marker_cleared(tmp_path: Path) -> None:
    """패키지 내부 marker가 지워지면(_index만 키 보유) 캐시 hit이 아니라 재주석."""
    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    meta_path = pkg / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["annotation"]["annotation_key"] = None  # 패키지 marker만 clear
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    stub = StubClient([_SHEET_OK, _WB_OK])
    r = annotate_package(pkg, client=stub)
    assert r.get("cached") is False and stub.responses == []


def test_cache_miss_when_body_violates_v2(tmp_path: Path) -> None:
    """generator·키가 멀쩡해도 본문이 V2(주소 실재성) 위반이면 캐시 hit이 아니라 재주석.

    evidence 훼손은 annotation_key(generator+파일sha 기반)를 바꾸지 않으므로 예전엔
    hit로 잡혔다. 이제 본문 계약(스키마+V2)을 캐시 경로에서도 확인해 재주석한다."""
    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    sem_path = pkg / "data/semantics.json"
    sem = json.loads(sem_path.read_text(encoding="utf-8"))
    sem["sheets"][0]["evidence"] = ["Data!ZZ999"]  # used range 밖 → V2 위반(키는 불변)
    sem_path.write_text(json.dumps(sem, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not next(c for c in verify_package(pkg).checks if c.name == "V2").ok

    stub = StubClient([_SHEET_OK, _WB_OK])  # hit이면 소비 안 됨
    r = annotate_package(pkg, client=stub)
    assert r.get("cached") is False and stub.responses == []  # 재주석함
    assert next(c for c in verify_package(pkg).checks if c.name == "V2").ok  # 복구


def test_cache_miss_when_body_violates_schema(tmp_path: Path) -> None:
    """본문이 스키마(additionalProperties:false 등)를 위반하면 캐시 hit이 아니라 재주석."""
    pkg = _pkg(tmp_path)
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))
    sem_path = pkg / "data/semantics.json"
    sem = json.loads(sem_path.read_text(encoding="utf-8"))
    sem["sheets"][0]["unexpected_field"] = 1  # additionalProperties:false 위반(키는 불변)
    sem_path.write_text(json.dumps(sem, ensure_ascii=False, indent=2), encoding="utf-8")

    stub = StubClient([_SHEET_OK, _WB_OK])
    r = annotate_package(pkg, client=stub)
    assert r.get("cached") is False and stub.responses == []
    v1 = next(c for c in verify_package(pkg).checks if c.name == "V1:data/semantics.json")
    assert v1.ok  # 재주석으로 스키마 유효 복구


def test_annotator_import_does_not_load_anthropic() -> None:
    """P1 경계: annotator import는 anthropic을 top-level로 불러오면 안 된다."""
    src = Path(annotator.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.strip() == "import anthropic":
            assert line[0].isspace(), "anthropic import는 함수 내부(지연)여야 함"


def test_build_client_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """무키 환경: 키 없으면 anthropic import 전에 RuntimeError(크래시 아님)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        annotator.build_anthropic_client("m")


# ── layout 입력 예산 발췌 + 컨텍스트 초과 처리(오너 계약) ─────────────
def test_excerpt_layout_row_boundary_marker_and_budget() -> None:
    """예산 초과 layout은 행 경계로 발췌 — 마커·예산 준수·행 중간 미절단·head/tail 보존."""
    from excel_to_skill.annotator import _excerpt_layout

    prefix = [
        "<!DOCTYPE html>", '<html lang="ko">', '<head><meta charset="utf-8">',
        "<style>x</style>", "</head>", "<body>", '<table data-sheet="S">',
    ]
    rows = [f'<tr><td data-cell="A{i}">{i:04d}-' + "x" * 40 + "</td></tr>"
            for i in range(1, 101)]
    suffix = ["</table>", "</body>", "</html>", ""]
    layout = "\n".join(prefix + rows + suffix)
    budget = 2000

    out, excerpted = _excerpt_layout(layout, budget)
    assert excerpted is True
    assert len(out) <= budget  # 예산 준수
    outrows = [ln for ln in out.split("\n") if ln.startswith("<tr")]
    marker = [ln for ln in outrows if "생략" in ln]
    data_rows = [ln for ln in outrows if "생략" not in ln]
    assert len(marker) == 1  # 생략 마커 정확히 1개
    assert data_rows and all(ln in set(rows) for ln in data_rows)  # 행 중간 미절단
    assert rows[0] in out and rows[-1] in out  # head+tail 보존
    omitted = int(re.search(r"가운데 (\d+)행", out).group(1))
    assert omitted == 100 - len(data_rows)  # 생략 행수 정확


def test_excerpt_layout_no_truncation_under_budget() -> None:
    """예산 이하 layout은 원본 그대로(발췌 아님)."""
    from excel_to_skill.annotator import _excerpt_layout

    layout = '<table data-sheet="S">\n<tr><td>a</td></tr>\n</table>\n'
    out, excerpted = _excerpt_layout(layout, 10_000)
    assert out == layout and excerpted is False


_WB_EMPTY = json.dumps({"workbook_claims": []}, ensure_ascii=False)


class SheetOverflowStub:
    """시트 호출에서 컨텍스트 초과를 흉내내는 스텁.

    always_overflow: 큰/작은 예산 모두 초과(→ 시트 제외). shrink_ok: 첫(큰) 예산엔
    초과, 두 번째(축소) 예산엔 성공. 그 외 시트는 used range 안 주소로 유효 응답.
    워크북은 빈 claims(항상 유효)로 계속 진행. 호출 횟수는 _seen에 시트별 누적.
    """

    _OVERFLOW = RuntimeError("prompt is too long: 9 tokens > 8 maximum")

    def __init__(self, *, always_overflow=frozenset(), shrink_ok=frozenset()) -> None:
        self.always = set(always_overflow)
        self.shrink_ok = set(shrink_ok)
        self._seen: dict[str, int] = {}

    def __call__(self, *, system: str, user: str, schema=None):
        if "워크북 단위" in user:
            return _WB_EMPTY
        name = re.search(r"시트명: (.+)", user).group(1).strip()
        dims = re.search(r"used range: (.*)", user).group(1).strip()
        cnt = self._seen.get(name, 0)
        self._seen[name] = cnt + 1
        if name in self.always:
            raise self._OVERFLOW
        if name in self.shrink_ok and cnt == 0:
            raise self._OVERFLOW
        topleft = (dims.split(":")[0] if dims else "A1") or "A1"
        return json.dumps(
            {"name": name, "purpose": "표", "evidence": [f"{name}!{topleft}"],
             "confidence": 0.9},
            ensure_ascii=False,
        )


def test_annotate_shrink_retry_then_success(tmp_path: Path) -> None:
    """시트 프롬프트가 컨텍스트 초과면 layout 예산을 줄여 1회 재시도해 성공한다."""
    pkg = _pkg(tmp_path)  # fx1 — 시트 Data 1개
    stub = SheetOverflowStub(shrink_ok={"Data"})
    res = annotate_package(pkg, client=stub)
    assert res["sheets"] == 1 and res["excluded"] == []
    assert stub._seen["Data"] == 2  # 첫 초과 → 축소 재시도 성공(2회 호출)
    assert next(c for c in verify_package(pkg).checks if c.name == "V2").ok


def test_annotate_excludes_oversized_sheet_others_continue(tmp_path: Path) -> None:
    """축소 후에도 초과하는 시트는 제외하고, 나머지 시트·워크북은 계속 주석."""
    pkg = _convert_one(FX_DIR / "fx2_refs.xlsx", tmp_path, force=True, cv=_converter_version())
    stub = SheetOverflowStub(always_overflow={"S1"})  # S1은 계속 초과 → 제외, S2는 성공
    res = annotate_package(pkg, client=stub)
    assert res["excluded"] == ["S1"] and res["sheets"] == 1
    assert stub._seen["S1"] == 2  # 큰+축소 두 예산 모두 시도 후 제외
    sem = json.loads((pkg / "data/semantics.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in sem["sheets"]] == ["S2"]  # S2만 남음
    # 부분 결과라도 산출물은 V2 통과(제외로 불량 evidence 없음)
    assert next(c for c in verify_package(pkg).checks if c.name == "V2").ok
    # partial → 완료 키 없음(승인 불가)
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    assert meta["annotation"]["annotation_key"] is None


def test_non_overflow_error_is_not_swallowed(tmp_path: Path) -> None:
    """컨텍스트 초과가 아닌 오류는 제외로 삼키지 않고 그대로 전파한다."""
    pkg = _pkg(tmp_path)

    class BoomStub:
        def __call__(self, *, system, user, schema=None):
            raise RuntimeError("service unavailable (500)")  # 초과 아님

    with pytest.raises(RuntimeError, match="service unavailable"):
        annotate_package(pkg, client=BoomStub())


# ── annotate --all 배치(오너 순서 2·3) ────────────────────────────
def _fake_index_root(base: Path, dirnames: list[str]) -> Path:
    """base/converted에 _index.json으로 dirnames를 등록한 root를 만든다(항목 필드 최소)."""
    from excel_to_skill import cache

    root = base / "converted"
    cache.save_index(root, {"index_version": 1, "entries": {d: {} for d in dirnames}})
    return root  # save_index가 parents=True로 폴더 생성


def test_annotate_all_counts_and_isolates_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """배치: 색인 기준 4범주 집계 + 실패 격리 + 폴더/meta 누락도 failed + 색인 밖 폴더 무시."""
    from excel_to_skill.cli import _annotate_all

    behaviors: dict[str, object] = {
        "pkg_ok": {"path": "x", "sheets": 1, "excluded": [], "cached": False},
        "pkg_cached": {"path": "x", "sheets": 2, "excluded": [], "cached": True},
        "pkg_excluded": {"path": "x", "sheets": 0, "excluded": ["Data"], "cached": False},
        "pkg_fail": "raise",
    }
    # 색인엔 위 4개 + ghost(폴더 없음) + nometa(폴더는 있지만 meta.json 없음) = 6개 등록
    root = _fake_index_root(
        tmp_path, [*behaviors, "pkg_ghost", "pkg_nometa"]
    )
    for name in behaviors:  # 실물 폴더 + meta.json
        (root / name).mkdir()
        (root / name / "meta.json").write_text("{}", encoding="utf-8")
    (root / "pkg_nometa").mkdir()  # 폴더만, meta.json 없음 → failed
    # pkg_ghost: 폴더 자체가 없음 → failed
    (root / "orphan_ondisk").mkdir()  # 색인 밖 폴더(+meta) → 무시(집계 안 됨)
    (root / "orphan_ondisk" / "meta.json").write_text("{}", encoding="utf-8")

    def fake(pkg, *, model=None, client=None, force=False, eprint=None):  # noqa: ANN001
        b = behaviors[pkg.name]
        if b == "raise":
            raise RuntimeError("boom")
        return b

    monkeypatch.setattr(annotator, "annotate_package", fake)
    s = _annotate_all(root, model=None, force=False, eprint=lambda *a: None)
    # 성공1·캐시1·제외1·실패3(fail+ghost+nometa)·총 6(색인 등록). orphan_ondisk는 미포함.
    assert s == {"ok": 1, "cached": 1, "excluded": 1, "failed": 3, "total": 6}


def test_annotate_all_cli_exit_and_stdout_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 계약: 성공/캐시만→exit 0, 제외/실패→exit 1, 0건→exit 1, stdout=요약 1줄(패키지명 없음)."""
    from excel_to_skill.cli import main

    def patch(behaviors: dict) -> None:
        def fake(pkg, *, model=None, client=None, force=False, eprint=None):  # noqa: ANN001
            b = behaviors[pkg.name]
            if b == "raise":
                raise RuntimeError("boom")
            return b
        monkeypatch.setattr(annotator, "annotate_package", fake)

    def build(label: str, names: list[str]) -> Path:
        root = _fake_index_root(tmp_path / label, names)
        for n in names:
            (root / n).mkdir()
            (root / n / "meta.json").write_text("{}", encoding="utf-8")
        return root

    # (A) 성공/캐시만 → exit 0, stdout 한 줄·패키지명 미포함
    rootA = build("A", ["pkg_ok", "pkg_cached"])
    patch({
        "pkg_ok": {"sheets": 1, "excluded": [], "cached": False},
        "pkg_cached": {"sheets": 1, "excluded": [], "cached": True},
    })
    rc = main(["annotate", str(rootA), "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip().count("\n") == 0 and out.strip()  # 정확히 한 줄
    assert "pkg_ok" not in out and "pkg_cached" not in out  # stdout에 패키지명 없음

    # (B) 제외 존재 → exit 1
    rootB = build("B", ["pkg_excluded"])
    patch({"pkg_excluded": {"sheets": 0, "excluded": ["Data"], "cached": False}})
    assert main(["annotate", str(rootB), "--all"]) == 1

    # (C) 실패 존재 → exit 1
    rootC = build("C", ["pkg_fail"])
    patch({"pkg_fail": "raise"})
    assert main(["annotate", str(rootC), "--all"]) == 1

    # (D) 대상 0건(빈 색인) → exit 1
    rootD = _fake_index_root(tmp_path / "D", [])
    assert main(["annotate", str(rootD), "--all"]) == 1


def test_annotate_all_all_cached_needs_no_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전건 캐시면 클라이언트 생성 없이(무키) 배치가 성립한다(캐시 hit 생략)."""
    from excel_to_skill.cli import _annotate_all

    pkg = _pkg(tmp_path)  # converted root = tmp_path
    annotate_package(pkg, client=StubClient([_SHEET_OK, _WB_OK]))  # 완료 주석
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # 무키
    s = _annotate_all(tmp_path, model=None, force=False, eprint=lambda *a: None)
    assert s == {"ok": 0, "cached": 1, "excluded": 0, "failed": 0, "total": 1}
