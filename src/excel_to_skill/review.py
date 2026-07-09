"""§7 review — 해석 계층 승인/반려(결정론, `anthropic` 미사용).

- `--approve`: 승인 **전 verify 전체 통과가 전제**(§8.1 "approve 전 필수 통과"). 통과하면
  `review.status=approved` + `reviewed_at`으로 갱신하고 **승인판 SKILL.md를 의미 기반으로
  재생성**한다. 통과 못 하면 승인 거부(변경 없음).
- `--reject`: `note` 필수. `rejected` + `reviewed_at` + `note`로 갱신하고 SKILL.md를
  **미승인 형태로 재생성**한다(⑥ 해석이 승인 의미를 노출하지 않도록).

SKILL.md 재생성은 패키지 파일만으로 수행한다(IR 없음 — `build_skill_md_from_package`).
이 모듈은 LLM을 쓰지 않으므로 `anthropic`을 import하지 않는다(P1 경계와 무관하게 결정론).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import cache
from .emit_skill_md import build_skill_md_from_package
from .meta import _now_iso, set_annotation


class ReviewError(RuntimeError):
    """승인/반려를 진행할 수 없는 상태(semantics 없음·V 검증 실패·note 누락 등)."""


def _semantics_path(pkg: Path) -> Path:
    f = pkg / "data" / "semantics.json"
    if not f.is_file():
        raise ReviewError("semantics.json 없음 — 먼저 annotate 하세요.")
    return f


def _regen_skill(pkg: Path) -> None:
    text = build_skill_md_from_package(pkg)
    with (pkg / "SKILL.md").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_semantics(path: Path, semantics: dict) -> None:
    path.write_text(
        json.dumps(semantics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def approve(pkg) -> dict:
    """패키지를 승인한다. verify 전체 통과가 전제. 반환: {"status","skill"}."""
    from .verify import verify_package  # 지연 import(순환 회피)

    pkg = Path(pkg)
    path = _semantics_path(pkg)

    # 승인 게이트 ①: 원본 없이 verify(→V3 skip). V1·V2·필수파일 등 전부 통과해야.
    result = verify_package(pkg, source=None)
    if not result.ok:
        fails = [f"{c.name}: {c.detail}" for c in result.checks if not c.skipped and not c.ok]
        raise ReviewError(f"verify 실패로 승인 거부 — {fails}")

    semantics = json.loads(path.read_text(encoding="utf-8"))

    # 승인 게이트 ②: **완료된 주석만 승인**(패키지-독립). annotation_key가 완료 marker
    # 이므로, semantics.generator+meta로 계산한 4성분 키가 **meta.annotation.annotation_key**
    # (패키지 내부 marker)와 같아야 한다. partial annotate는 키를 남기지 않으므로(None)
    # 불일치→거부. _index가 아니라 패키지 파일만 보므로 폴더를 옮겨도 승인이 동작한다.
    gen = semantics.get("generator", {})
    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    expected = cache.annotation_key(
        meta.get("source", {}).get("sha256", ""),
        gen.get("annotator_version", ""),
        gen.get("model", ""),
        gen.get("prompt_sha", ""),
    )
    recorded = meta.get("annotation", {}).get("annotation_key")
    if recorded != expected:
        raise ReviewError(
            "완료되지 않은 주석은 승인할 수 없습니다"
            "(annotation_key 미완료/불일치 — annotate를 다시 완료하세요)."
        )

    semantics["review"] = {"status": "approved", "reviewed_at": _now_iso(), "note": None}
    _write_semantics(path, semantics)
    _sync_meta(pkg, semantics, "approved")
    _regen_skill(pkg)  # 승인판 SKILL.md(⑥ 해석 렌더)
    return {"status": "approved", "skill": str(pkg / "SKILL.md")}


def reject(pkg, *, note: str | None) -> dict:
    """패키지를 반려한다. note 필수. SKILL.md는 미승인 형태로 재생성."""
    pkg = Path(pkg)
    path = _semantics_path(pkg)
    if not (note and note.strip()):
        raise ReviewError("--reject에는 --note(반려 사유)가 필수입니다.")

    semantics = json.loads(path.read_text(encoding="utf-8"))
    semantics["review"] = {
        "status": "rejected",
        "reviewed_at": _now_iso(),
        "note": note.strip(),
    }
    _write_semantics(path, semantics)
    _sync_meta(pkg, semantics, "rejected")
    _regen_skill(pkg)  # rejected → 미승인 SKILL.md
    return {"status": "rejected", "skill": str(pkg / "SKILL.md")}


def _sync_meta(pkg: Path, semantics: dict, status: str) -> None:
    """meta.annotation과 _index.json을 semantics 상태와 일치시킨다.

    meta.annotation(present·review_status·버전)과 색인의 review_status를 함께 갱신해,
    세 출처(semantics.review / meta.annotation / _index)가 어긋나지 않게 한다.
    """
    av = semantics.get("generator", {}).get("annotator_version")
    set_annotation(pkg, present=True, annotator_version=av, review_status=status)
    cache.update_annotation(pkg.parent, pkg.name, review_status=status)
