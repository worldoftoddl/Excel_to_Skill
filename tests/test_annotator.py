"""M3 2단계: 어노테이터 테스트(§7) — 클라이언트 주입으로 LLM 없이 결정론 고정.

실 anthropic 호출·API 키·패키지 설치 없이, 정해진 응답을 돌려주는 스텁 클라이언트로
happy path / 스키마불일치 1회 재시도 / 단위 제외 / P1 경계 / 무키 방어를 못박는다.
"""
from __future__ import annotations

import hashlib
import json
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

    def __call__(self, *, system: str, user: str) -> str:
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
