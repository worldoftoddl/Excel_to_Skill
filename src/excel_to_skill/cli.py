"""§3 CLI — `convert`(결정론 계층 조립)과 후속 스텁.

cli 1차(M1)는 흩어진 방출기를 한 명령으로 묶어 패키지 폴더를 만든다:

    converted/{stem_slug}_{sha12}/
    ├── meta.json
    └── data/{cells.jsonl, references.json, diagnostics.json}

`convert`·`verify`·`annotate`(§7 draft 생성)·`review`(승인/반려 + 승인판 SKILL.md
재생성)가 구현돼 있다. `annotate`만 `annotator`를 지연 import해 anthropic 경계(P1)를
convert/verify/review 경로 밖에 둔다(review는 결정론이라 anthropic 무관).

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
import json
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


def _should_inherit(
    probe: "cache.CacheProbe", old_pkg: Path, conv_params: dict
) -> bool:
    """§6 승계 조건: **converter_version만** 올랐고 기존이 완료 주석을 가진 실재 패키지.

    - `version_changed`이되 **conversion_params까지 완전히 동일**해야 한다. probe가
      version을 params보다 먼저 보므로, 버전과 max_rows/full_names가 동시에 바뀌면
      reason이 version_changed로 나온다 → 명시적으로 conv_params 동일을 재확인한다.
    - 완료 marker의 권위는 **패키지 내부 `meta.annotation.annotation_key`**(4a). 이 값이
      non-null이고 `_index.annotation_key`와 일치할 때만 완료로 본다.
    """
    entry = probe.entry
    if not (probe.reason == "version_changed" and entry is not None):
        return False
    if entry.get("conversion_params") != conv_params:
        return False  # 버전+옵션 동시 변경 → 승계 대상 아님
    if entry.get("sha256") != probe.sha256:  # 12자 접두 충돌 방어
        return False
    idx_key = entry.get("annotation_key")
    sem_path = old_pkg / "data" / "semantics.json"
    if not idx_key or not sem_path.is_file():
        return False
    try:
        meta = json.loads((old_pkg / "meta.json").read_text(encoding="utf-8"))
        sem = json.loads(sem_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    meta_key = meta.get("annotation", {}).get("annotation_key")
    # 완료 판정을 approve·verify와 동일 기준으로: semantics.generator+meta.source.sha256
    # 으로 재계산한 키까지 일치해야 한다(훼손된 generator/키 조합은 승계 거부 → verify가
    # 이미 실패시키는 semantics를 새 패키지로 번지지 않게).
    gen = sem.get("generator", {})
    expected = cache.annotation_key(
        meta.get("source", {}).get("sha256", ""),
        gen.get("annotator_version", ""),
        gen.get("model", ""),
        gen.get("prompt_sha", ""),
    )
    return bool(meta_key) and meta_key == idx_key == expected


def _inherit_semantics(
    old_pkg: Path, staging: Path, *, annotation_key: str
) -> tuple[str, str]:
    """구 semantics를 staging으로 이월하고 V2 재검증(§6). 반환 (annotation_key, review_status).

    이월된 evidence를 새 결정론 계층(staging의 meta.dimensions)으로 재검사해, 실패하면
    review.status를 draft로 강등하고 review.note에 사유를 남긴다(annotation_key는 완료
    marker라 유지 — 재승인은 approve의 verify 게이트가 V2로 다시 막는다). 이월 상태에
    맞춰 meta.annotation과 SKILL.md(승인판/미승인)를 재생성한다.
    """
    from .emit_skill_md import build_skill_md_from_package
    from .evidence import collect_evidence_problems
    from .meta import set_annotation

    sem = json.loads((old_pkg / "data/semantics.json").read_text(encoding="utf-8"))
    staging_meta = json.loads((staging / "meta.json").read_text(encoding="utf-8"))
    try:
        problems = collect_evidence_problems(sem, staging_meta)
    except NotImplementedError:
        problems = []

    review = sem.setdefault("review", {})
    if problems:
        review["status"] = "draft"
        review["reviewed_at"] = None
        review["note"] = f"승계 후 V2 재검증 실패로 draft 강등: {problems[:5]}"
        _eprint(f"[승계] {old_pkg.name}: V2 재검증 실패 {len(problems)}건 → draft 강등")
        status = "draft"
    else:
        status = review.get("status", "draft")
        _eprint(f"[승계] {old_pkg.name}: semantics 승계 · V2 통과(status={status})")

    (staging / "data" / "semantics.json").write_text(
        json.dumps(sem, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    av = sem.get("generator", {}).get("annotator_version")
    set_annotation(
        staging, present=True, annotator_version=av,
        review_status=status, annotation_key=annotation_key,
    )
    with (staging / "SKILL.md").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_skill_md_from_package(staging))
    return annotation_key, status


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
    carried_key: str | None = None
    carried_status: str | None = None
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
        # §6 승계: converter_version만 오른 재변환이면 완료된 구 semantics를 이월한다
        # (구 패키지 삭제 전에 읽어 staging으로 옮기고, V2 재검증·SKILL 재생성).
        if _should_inherit(probe, final, conv_params):
            carried_key, carried_status = _inherit_semantics(
                final, staging, annotation_key=probe.entry["annotation_key"]
            )

        # 여기까지 성공 → 원자적 교체(같은 파일시스템 내 rename)
        _safe_rmtree(final, root)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # 반쪽 폴더 정리
        raise

    # 최종 폴더가 선 뒤에만 색인 기록 (실패 시 _index.json 불변). 승계 시 주석 키·
    # 리뷰 상태를 이월값으로 기록(미승계면 None — 새 결정론 패키지).
    cache.record(
        root, src, sha256=probe.sha256, converter_version=cv,
        conversion_params=conv_params, generated_at=gen,
        annotation_key=carried_key, review_status=carried_status,
    )
    return final


def _warn_unsupported(args: argparse.Namespace) -> None:
    """이번 범위 밖·아직 무의미한 플래그를 조용히 무시하지 않고 stderr로 밝힌다."""
    if not args.no_annotate:
        _eprint(
            "[안내] convert는 결정론 계층만 생성합니다. "
            "해석 계층은 annotate 명령을 사용하세요."
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


def _annotate_all(
    root: Path,
    *,
    model: str | None,
    force: bool,
    client_factory=None,
    eprint=_eprint,
) -> dict:
    """converted_root 아래 모든 패키지(meta.json 보유 폴더)를 주석한다(배치).

    - **기준 목록은 `_index.json` 등록 항목**: 폴더/meta.json이 훼손·누락된 패키지도
      조용히 빠지지 않고 **failed로 집계**된다(색인에 있으나 실물이 없는 상태를 드러냄).
    - **파일별 실패 격리**: 한 패키지의 예외가 배치를 멈추지 않는다(실패로 집계+stderr).
    - **캐시 hit 생략**: annotate_package가 완료 주석이면 cached=True를 돌려주고
      클라이언트도 만들지 않는다 — 배치는 세기만 한다.
    - **집계 4범주**: 성공(신규·제외 없음)·캐시·제외(부분 실패)·실패(예외/누락) + 총계.

    client_factory(pkg)를 주면 패키지마다 클라이언트를 만들어 넘긴다(테스트 주입용).
    실 운영에선 None — annotate_package가 miss일 때만 지연 생성한다(전건 캐시면 무키로도 성립).
    반환: {"ok","cached","excluded","failed","total"}. total은 색인 등록 항목 수.
    """
    from .annotator import annotate_package

    dirnames = sorted(cache.load_index(root).get("entries", {}))
    ok = cached = excluded = failed = 0
    for dirname in dirnames:
        pkg = root / dirname
        if not pkg.is_dir() or not (pkg / "meta.json").is_file():
            failed += 1  # 색인엔 있으나 폴더/meta.json 누락 — 조용히 빼지 않는다
            eprint(f"[annotate 실패] {dirname}: 폴더/meta.json 누락(색인 등록됨)")
            continue
        try:
            client = client_factory(pkg) if client_factory is not None else None
            res = annotate_package(
                pkg, model=model, client=client, force=force, eprint=eprint
            )
        except Exception as e:  # 파일별 실패 격리 — 다음 패키지 계속
            failed += 1
            eprint(f"[annotate 실패] {pkg.name}: {e!r}")
            continue
        if res.get("cached"):
            cached += 1
            eprint(f"[annotate] {pkg.name}: 캐시 hit(생략)")
        elif res["excluded"]:
            excluded += 1
            eprint(f"[annotate] {pkg.name}: 제외 {len(res['excluded'])}건 → {res['excluded']}")
        else:
            ok += 1
            eprint(f"[annotate] {pkg.name}: sheets {res['sheets']}건 주석(draft)")
    return {
        "ok": ok, "cached": cached, "excluded": excluded,
        "failed": failed, "total": len(dirnames),
    }


def _cmd_annotate(args: argparse.Namespace) -> int:
    # annotator는 여기서만 지연 import — convert/verify 경로가 anthropic을 건드리지
    # 않게 하고(P1 경계), anthropic 미설치 환경에서도 다른 명령이 살아 있게 한다.
    from .annotator import annotate_package

    pkg = Path(args.path)
    if args.all:
        if not pkg.is_dir():
            _eprint(f"[오류] --all 대상이 디렉터리(converted_root)가 아님: {pkg}")
            return 1
        summary = _annotate_all(pkg, model=args.model, force=args.force)
        # stdout = 요약 카운트 한 줄(원문·경로 미노출). per-package 상태는 stderr.
        print(
            f"성공 {summary['ok']} · 캐시 {summary['cached']} · "
            f"제외 {summary['excluded']} · 실패 {summary['failed']} (총 {summary['total']})"
        )
        if summary["total"] == 0:
            _eprint(f"[오류] 주석 대상 패키지 없음(meta.json 보유 폴더): {pkg}")
            return 1
        _eprint(
            f"[annotate --all] 성공 {summary['ok']} · 캐시 {summary['cached']} · "
            f"제외 {summary['excluded']} · 실패 {summary['failed']} (총 {summary['total']})"
        )
        # 일부라도 실패/제외면 비영 exit(단일 annotate가 excluded에 exit 1 하는 것과 일관).
        return 1 if (summary["failed"] or summary["excluded"]) else 0

    if not pkg.is_dir() or not (pkg / "meta.json").is_file():
        _eprint(f"[오류] 패키지 폴더가 아님(meta.json 없음): {pkg}")
        return 1
    try:
        result = annotate_package(pkg, model=args.model, force=args.force)
    except RuntimeError as e:  # 무키 등 — 명확히 실패로 보고(크래시 아님)
        _eprint(f"[annotate 실패] {e}")
        return 1
    except Exception as e:
        _eprint(f"[annotate 실패] {pkg.name}: {e!r}")
        return 1

    print(result["path"])  # stdout = 산출 semantics.json 경로
    if result.get("cached"):
        _eprint(f"[annotate] {pkg.name}: 캐시 hit — 재주석 생략(--force로 재생성)")
        return 0
    excluded = result["excluded"]
    if excluded:
        _eprint(f"[annotate] 제외된 단위 {len(excluded)}건: {excluded}")
    _eprint(
        f"[annotate] {pkg.name}: sheets {result['sheets']}건 주석"
        f"{' · 일부 제외' if excluded else ''} (status=draft — review 전 verify V2 권장)"
    )
    return 1 if excluded else 0


def _cmd_review(args: argparse.Namespace) -> int:
    from .review import ReviewError, approve, reject  # 결정론 — anthropic 무관

    pkg = Path(args.path)
    if not pkg.is_dir() or not (pkg / "meta.json").is_file():
        _eprint(f"[오류] 패키지 폴더가 아님(meta.json 없음): {pkg}")
        return 1
    if args.approve == args.reject:  # 둘 다 또는 둘 다 아님
        _eprint("[오류] --approve 또는 --reject 중 정확히 하나를 지정하세요.")
        return 1
    try:
        res = approve(pkg) if args.approve else reject(pkg, note=args.note)
    except ReviewError as e:
        _eprint(f"[review 실패] {e}")
        return 1
    except Exception as e:
        _eprint(f"[review 실패] {pkg.name}: {e!r}")
        return 1
    print(res["skill"])  # stdout = 재생성된 SKILL.md 경로
    _eprint(f"[review] {pkg.name}: status={res['status']} · SKILL.md 재생성")
    return 0


def _cmd_consume(args: argparse.Namespace) -> int:
    """소비 인터페이스(overview/inspect/search/refs) — 결과 JSON을 stdout으로."""
    from .consume import ConsumeError, inspect, overview, refs, search

    pkg = Path(args.path)
    if not pkg.is_dir() or not (pkg / "meta.json").is_file():
        _eprint(f"[오류] 패키지 폴더가 아님(meta.json 없음): {pkg}")
        return 1
    lim = {} if getattr(args, "limit", None) is None else {"limit": args.limit}
    try:
        if args.cmd == "overview":
            result = overview(pkg, sheet=getattr(args, "sheet", None), **lim)
        elif args.cmd == "inspect":
            result = inspect(pkg, sheet=args.sheet, range=args.range, cell=args.cell, **lim)
        elif args.cmd == "search":
            result = search(pkg, query=args.query, sheet=args.sheet, **lim)
        else:  # refs
            result = refs(pkg, cell=args.cell, **lim)
    except ConsumeError as e:
        _eprint(f"[{args.cmd} 실패] {e}")
        return 1
    print(json.dumps(result, ensure_ascii=False))  # stdout = 조회 결과 JSON 한 줄
    return 0


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
    a.add_argument("path", help="주석할 패키지 폴더(또는 --all 시 converted_root)")
    a.add_argument(
        "--all", action="store_true",
        help="converted_root 아래 모든 패키지 일괄 주석(실패 격리·집계)",
    )
    a.add_argument("--model", default=None, help="어노테이터 모델(기본은 코드 상수)")
    a.add_argument("--force", action="store_true", help="주석 캐시 무시하고 재주석")

    r = sub.add_parser("review", help="해석 계층 승인(--approve)/반려(--reject)")
    r.add_argument("path", help="검토할 패키지 폴더")
    r.add_argument("--approve", action="store_true", help="승인(verify 통과 전제)")
    r.add_argument("--reject", action="store_true", help="반려(--note 필수)")
    r.add_argument("--note", default=None, help="반려 사유(--reject 시 필수)")

    # 소비 인터페이스(§10) — Agent가 '개요→시트→셀'로 단계 조회(원본 JSON 통째 로드 금지)
    o = sub.add_parser("overview", help="패키지 개요(셀 원문 없이 구조·해석 요약)")
    o.add_argument("path", help="조회할 패키지 폴더")
    o.add_argument("--sheet", default=None, help="지정 시트의 구간 상세(승인 시)")
    o.add_argument("--limit", type=int, default=None, help="--sheet 구간 상한(기본 100)")

    ins = sub.add_parser("inspect", help="지정 시트의 범위/셀 원장 조회")
    ins.add_argument("path", help="조회할 패키지 폴더")
    ins.add_argument("--sheet", required=True, help="시트명")
    g = ins.add_mutually_exclusive_group()
    g.add_argument("--range", default=None, help="범위(A1:B10). 생략 시 시트 전체")
    g.add_argument("--cell", default=None, help="단일 셀(A1)")
    ins.add_argument("--limit", type=int, default=None, help="반환 셀 상한(기본 200)")

    s = sub.add_parser("search", help="값·수식 부분일치 조회")
    s.add_argument("path", help="조회할 패키지 폴더")
    s.add_argument("--query", required=True, help="찾을 문자열(대소문자 무시)")
    s.add_argument("--sheet", default=None, help="특정 시트로 제한(선택)")
    s.add_argument("--limit", type=int, default=None, help="매치 상한(기본 30)")

    rf = sub.add_parser("refs", help="지정 셀의 출입 참조 엣지 조회")
    rf.add_argument("path", help="조회할 패키지 폴더")
    rf.add_argument("--cell", required=True, help="절대 주소(Sheet!A1)")
    rf.add_argument("--limit", type=int, default=None, help="방향별 엣지 상한(기본 100)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "convert":
        return _cmd_convert(args)
    if args.cmd == "verify":
        return _cmd_verify(args)
    if args.cmd == "annotate":
        return _cmd_annotate(args)
    if args.cmd == "review":
        return _cmd_review(args)
    if args.cmd in ("overview", "inspect", "search", "refs"):
        return _cmd_consume(args)
    _eprint(f"'{args.cmd}' 은(는) 알 수 없는 명령입니다.")
    return 2


if __name__ == "__main__":  # python -m excel_to_skill.cli
    sys.exit(main())
