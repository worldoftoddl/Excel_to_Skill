"""§3 CLI — `convert`(결정론 계층 조립)과 후속 스텁.

cli 1차(M1)는 흩어진 방출기를 한 명령으로 묶어 패키지 폴더를 만든다:

    converted/{stem_slug}_{sha12}/
    ├── meta.json
    └── data/{cells.jsonl, references.json, diagnostics.json}

`convert`·`verify`·`annotate`(§7 해석 계층 draft 생성)는 구현됐고, `review`(승인·
승인판 SKILL.md 재생성)는 M3 3단계 스텁(exit 2)이다. `annotate`는 `annotator`를
지연 import해 anthropic 경계(P1)를 convert/verify 경로 밖에 둔다.

조립 계약(오너 확정):
- **stdout은 패키지 경로만.** 진행·cache 사유·경고·오류는 전부 stderr.
- **generated_at은 한 변환에서 1회 계산**해 meta.json과 _index.json에 같은 값.
- diagnostics에는 write_references가 돌려준 refs dict를 넘겨 재사용.
- **원자적 생성**: 임시 폴더(.staging_*)에 전부 쓴 뒤 성공하면 최종 폴더로 rename,
  그 다음에야 cache.record. 도중 실패 시 임시 폴더만 지우고 _index.json은 손대지 않음
  → 반쪽 폴더가 캐시 hit로 잡히지 않는다.
- **삭제 방어**: 폴더 삭제 대상은 반드시 출력 루트 내부로 한정(root 바깥 거부).
- hit이면 어떤 파일도 다시 쓰지 않고 기존 경로만 stdout.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import cache
from .emit_cells import write_cells_jsonl
from .emit_defined_names import write_defined_names_full
from .emit_diag import write_diagnostics
from .emit_html import DEFAULT_MAX_ROWS, write_layout
from .emit_refs import write_references
from .emit_skill_md import write_skill_md
from .extractor import extract_workbook
from .meta import _converter_version, _now_iso, write_meta

_EXTS = (".xlsx", ".xls")  # docx는 M4


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _within_root(path: Path, root: Path) -> bool:
    """path가 root 내부(자식 이하)인가 — root 자신·바깥은 False."""
    root_r = root.resolve()
    p = path.resolve()
    return p != root_r and root_r in p.parents


def _safe_rmtree(target: Path, root: Path) -> None:
    """root 내부일 때만 삭제한다. 바깥 경로는 거부(방어)."""
    if not _within_root(target, root):
        raise RuntimeError(f"삭제 거부: {target} 는 출력 루트 {root} 밖")
    if target.exists():
        shutil.rmtree(target)


def _convert_one(
    src: Path,
    root: Path,
    *,
    force: bool,
    cv: str,
    max_rows: int = DEFAULT_MAX_ROWS,
    full_names: bool = False,
) -> Path:
    """파일 하나를 패키지로 변환하고 최종 폴더 경로를 돌려준다.

    hit이면 재생성 없이 기존 경로. miss/force면 임시 폴더에 조립 후 원자적 교체.
    조립 순서: meta → cells → references → **layout(절단 계산) → diagnostics
    (truncations 반영) → SKILL.md**. 원장(cells)은 절대 자르지 않는다.
    full_names면 data/defined_names_full.json(전량 덤프)을 추가로 쓰고 diagnostics의
    full_dump_present=true로 맞춘다. meta.conversion_params.full_names가 그 조건을
    자기증언해 V3 재변환이 같은 조건으로 재현한다.
    """
    # 변환 파라미터도 캐시 키의 일부다(옵션이 바뀌면 산출이 달라짐). meta의 것과
    # 같은 형태·키 순서로 만들어 probe 대조·색인 기록에 함께 쓴다.
    conv_params = {"max_rows": max_rows, "full_names": full_names}
    probe = cache.probe(
        root, src, converter_version=cv, conversion_params=conv_params, force=force
    )
    if probe.hit:
        _eprint(f"[cache hit] {src.name} → {probe.package_dir} (재생성 생략)")
        return probe.package_path

    _eprint(f"[cache miss:{probe.reason}] {src.name} → 생성")
    ir = extract_workbook(src)  # 추출은 miss일 때만
    gen = _now_iso()  # 한 변환 = 한 시각 (meta·index 공유)

    final = probe.package_path
    staging = root / (".staging_" + probe.package_dir)
    _safe_rmtree(staging, root)  # 이전 실패 잔재 제거
    root.mkdir(parents=True, exist_ok=True)
    try:
        staging.mkdir(parents=True)
        data = staging / "data"
        data.mkdir()
        meta_doc = write_meta(
            ir,
            staging / "meta.json",
            generated_at=gen,
            max_rows=max_rows,
            full_names=full_names,
        )
        write_cells_jsonl(ir, data / "cells.jsonl")
        refs = write_references(ir, data / "references.json")
        # --full-names면 정의이름 전량 덤프(전건 + 값 전문, 이메일만 P7 마스킹).
        if full_names:
            write_defined_names_full(ir, data / "defined_names_full.json")
        # layout을 먼저 써서 절단 기록을 받고, 그걸 diagnostics에 반영한다.
        filenames, truncations = write_layout(
            ir, staging / "layout", max_rows=max_rows
        )
        diag_doc = write_diagnostics(
            ir,
            data / "diagnostics.json",
            references=refs,
            truncations=truncations,
            full_names=full_names,
        )
        write_skill_md(
            ir,
            staging / "SKILL.md",
            meta=meta_doc,
            references=refs,
            diagnostics=diag_doc,
            layout_filenames=filenames,
        )
        # 여기까지 성공 → 원자적 교체(같은 파일시스템 내 rename)
        _safe_rmtree(final, root)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # 반쪽 폴더 정리
        raise

    # 최종 폴더가 선 뒤에만 색인 기록 (실패 시 _index.json 불변)
    cache.record(
        root, src, sha256=probe.sha256, converter_version=cv,
        conversion_params=conv_params, generated_at=gen,
    )
    return final


def _warn_unsupported(args: argparse.Namespace) -> None:
    """이번 범위 밖·아직 무의미한 플래그를 조용히 무시하지 않고 stderr로 밝힌다."""
    if not args.no_annotate:
        _eprint(
            "[안내] 주석기(해석 계층) 미구현 — 결정론 계층만 생성됩니다"
            "(현재 기본, =--no-annotate)."
        )
    if args.force_annotate:
        _eprint("[안내] --force-annotate: convert 범위 밖 — 무시(annotate는 별도 명령).")
    if args.model is not None:
        _eprint("[안내] --model: convert에는 무효 — annotate 서브커맨드에서 사용하세요.")


def _cmd_convert(args: argparse.Namespace) -> int:
    root = Path(args.out)
    cv = _converter_version()
    max_rows = args.max_rows
    full_names = args.full_names
    _warn_unsupported(args)
    target = Path(args.path)

    if args.all:
        if not target.is_dir():
            _eprint(f"[오류] --all 대상이 디렉터리가 아님: {target}")
            return 1
        files = sorted(
            f
            for ext in _EXTS
            for f in target.glob(f"*{ext}")
            if not f.name.startswith("~$")  # Excel 임시잠금 제외
        )
        if not files:
            _eprint(f"[오류] 변환 대상 없음(*.xlsx/*.xls): {target}")
            return 1
        failed = 0
        for f in files:
            try:
                print(
                    _convert_one(
                        f, root, force=args.force, cv=cv,
                        max_rows=max_rows, full_names=full_names,
                    )
                )
            except Exception as e:  # 한 파일 실패해도 배치 계속
                failed += 1
                _eprint(f"[실패] {f.name}: {e!r}")
        _eprint(f"[요약] {len(files)}건 중 성공 {len(files) - failed} · 실패 {failed}")
        return 1 if failed else 0

    # 단일 파일
    if not target.is_file():
        _eprint(f"[오류] 파일이 아님: {target}")
        return 1
    try:
        print(
            _convert_one(
                target, root, force=args.force, cv=cv,
                max_rows=max_rows, full_names=full_names,
            )
        )
        return 0
    except Exception as e:
        _eprint(f"[실패] {target.name}: {e!r}")
        return 1


def _cmd_verify(args: argparse.Namespace) -> int:
    from .verify import verify_package

    pkg = Path(args.path)
    if not pkg.is_dir():
        _eprint(f"[오류] 패키지 폴더가 아님: {pkg}")
        return 1
    src = Path(args.source) if args.source else None
    if src is not None and not src.is_file():
        _eprint(f"[오류] --source 파일 없음: {src}")
        return 1

    result = verify_package(pkg, source=src)
    for c in result.checks:  # 리포트는 stdout
        mark = "SKIP" if c.skipped else ("PASS" if c.ok else "FAIL")
        print(f"  [{mark}] {c.name}: {c.detail}")
    print(f"verify: {'통과' if result.ok else '실패'} ({pkg})")
    if not result.ok:  # 실패 사유는 stderr, exit code가 권위
        _eprint(f"[verify 실패] {pkg}")
    return 0 if result.ok else 1


def _cmd_annotate(args: argparse.Namespace) -> int:
    # annotator는 여기서만 지연 import — convert/verify 경로가 anthropic을 건드리지
    # 않게 하고(P1 경계), anthropic 미설치 환경에서도 다른 명령이 살아 있게 한다.
    from .annotator import annotate_package

    pkg = Path(args.path)
    if not pkg.is_dir() or not (pkg / "meta.json").is_file():
        _eprint(f"[오류] 패키지 폴더가 아님(meta.json 없음): {pkg}")
        return 1
    try:
        result = annotate_package(pkg, model=args.model)
    except RuntimeError as e:  # 무키 등 — 명확히 실패로 보고(크래시 아님)
        _eprint(f"[annotate 실패] {e}")
        return 1
    except Exception as e:
        _eprint(f"[annotate 실패] {pkg.name}: {e!r}")
        return 1

    print(result["path"])  # stdout = 산출 semantics.json 경로
    excluded = result["excluded"]
    if excluded:
        _eprint(f"[annotate] 제외된 단위 {len(excluded)}건: {excluded}")
    _eprint(
        f"[annotate] {pkg.name}: sheets {result['sheets']}건 주석"
        f"{' · 일부 제외' if excluded else ''} (status=draft — review 전 verify V2 권장)"
    )
    return 1 if excluded else 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="excel-to-skill")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="파일/디렉터리를 스킬 패키지로 변환")
    c.add_argument("path", help="원본 파일(또는 --all 시 디렉터리)")
    c.add_argument("--all", action="store_true", help="디렉터리 최상위 일괄 변환")
    c.add_argument("--out", default="./converted", help="출력 루트(기본 ./converted)")
    c.add_argument("--force", action="store_true", help="캐시 무시하고 재생성")
    c.add_argument("--no-annotate", action="store_true", help="해석 계층 생략(현재 기본)")
    c.add_argument("--force-annotate", action="store_true", help="(미구현)")
    c.add_argument(
        "--full-names",
        action="store_true",
        help="정의된 이름 전량 덤프(data/defined_names_full.json) 방출",
    )
    c.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=f"layout HTML 표 행 상한(기본 {DEFAULT_MAX_ROWS}). 초과 시 첫 N+말미 5행",
    )
    c.add_argument("--model", default=None, help="(미구현)")

    v = sub.add_parser("verify", help="패키지 계약 검증(V1 스키마 + V3 재현성)")
    v.add_argument("path", help="검증할 패키지 폴더")
    v.add_argument("--source", default=None, help="원본 파일(주면 V3 재현성 검증)")

    a = sub.add_parser("annotate", help="패키지에 해석 계층(semantics.json draft) 생성")
    a.add_argument("path", help="주석할 패키지 폴더")
    a.add_argument("--model", default=None, help="어노테이터 모델(기본은 코드 상수)")

    # review는 다음 단계(M3 3단계) — 아직 스텁(exit 2)
    sub.add_parser("review", help="(아직 구현되지 않음 — M3 3단계)").add_argument(
        "rest", nargs=argparse.REMAINDER
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "convert":
        return _cmd_convert(args)
    if args.cmd == "verify":
        return _cmd_verify(args)
    if args.cmd == "annotate":
        return _cmd_annotate(args)
    # review — 등록만 된 스텁 (M3 3단계)
    _eprint(f"'{args.cmd}' 은(는) 아직 구현되지 않았습니다 (M3 3단계).")
    return 2


if __name__ == "__main__":  # python -m excel_to_skill.cli
    sys.exit(main())
