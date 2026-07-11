"""§7 어노테이터 — 해석 계층(data/semantics.json) 생성기.

**P1 물리적 경계: `anthropic`을 import하는 유일한 모듈이다.** 그것도
`build_anthropic_client()` 안에서만 지연 import하므로, 클라이언트를 주입해 쓰는 경로
(테스트·미리보기)는 anthropic 미설치·무네트워크에서도 돈다. convert/verify는 이 모듈을
아예 건드리지 않고, cli의 `annotate` 서브커맨드만 지연 import한다.

동작(§7):
  - 입력 = 패키지의 layout/*.html(구조) + data/cells.jsonl(주소 근거) + references(요약).
  - **시트 단위**로 호출해 sheets[] 항목을 만들고, 마지막에 **워크북 단위** 1회로
    workbook_claims를 만든다(호출 순서: meta.sheets 순 → workbook).
  - **layout 입력 예산**: 시트 프롬프트의 layout HTML을 보수적 char 예산으로 **행 경계
    발췌**(앞+뒤 보존·가운데 생략 마커·모델에 발췌 고지)해 컨텍스트를 넘지 않게 한다.
    그래도 컨텍스트 초과면 더 작은 예산으로 1회 축소 재시도, 최종 초과면 그 시트만 제외.
  - temperature 0, 응답은 JSON만. 받은 JSON을 semantics.schema.json의 해당 하위 스키마로
    검증하고, 불일치면 오류를 첨부해 **1회 재시도**, 재실패면 그 단위를 결과에서 **제외**
    하고 stderr로 보고한다(진단 파일에는 LLM 실패를 남기지 않는다).
  - 산출은 status="draft"인 semantics.json. 승인/재생성은 review 단계(별도).

클라이언트 계약: `client(*, system: str, user: str, schema: dict) -> dict | str`
(모델·temperature는 팩토리가 캡슐화). structured output 경로는 스키마-유효 dict를,
텍스트 폴백/주입 스텁은 문자열을 돌려주며 `_call_unit`이 둘 다 받아 검증한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import jsonschema

from . import cache
from .evidence import collect_evidence_problems
from .meta import _now_iso, set_annotation
from .resources import PROMPT_DIR, SCHEMA_DIR

_PROMPT_PATH = PROMPT_DIR / "annotator_v1.md"
_SCHEMA_DIR = SCHEMA_DIR

ANNOTATOR_VERSION = "0.2.0"  # 0.2.0: 시트 layout 입력 예산 발췌·컨텍스트 초과 처리(캐시 무효화)
DEFAULT_MODEL = "claude-sonnet-4-5"  # §7 기본 모델명 — 유일 출처(코드 상수 1곳 + README)
TEMPERATURE = 0
_MAX_RETRY = 1  # 스키마 불일치 시 오류 첨부 1회 재시도
_MAX_TOKENS = 4096
_CELL_CAP = 400  # 시트당 프롬프트에 넣는 원장 줄 상한(발췌 — 구현 재량)
# 시트 단위 프롬프트의 layout HTML char 예산(보수적 상한). 초과 시 행 경계로 발췌하고,
# 그래도 컨텍스트가 초과되면 더 작은 예산으로 1회 축소 재시도, 최종 초과면 그 시트만 제외.
_LAYOUT_BUDGET = 150_000
_LAYOUT_BUDGET_MIN = 50_000


def _prompt_text_and_sha() -> tuple[str, str]:
    raw = _PROMPT_PATH.read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


# ── 실 클라이언트 팩토리 (anthropic·langsmith import는 여기서만) ──────
_TOOL_NAME = "emit_semantics"  # structured output용 강제 도구 이름


def build_anthropic_client(
    model: str,
    *,
    tool_name: str = _TOOL_NAME,
    max_tokens: int = _MAX_TOKENS,
):
    """실 anthropic 클라이언트를 `(*, system, user, schema) -> dict` 콜러블로 감싼다.

    - **Structured output**: 응답 스키마를 도구 `input_schema`로 주고 `tool_choice`로
      강제해, 모델이 스키마-유효 JSON을 구조적으로 방출하게 한다(텍스트 파싱 실패 제거).
      반환은 tool_use 블록의 `input`(dict).
    - **프롬프트 캐싱**: 상수인 system 프롬프트에 `cache_control(ephemeral)`을 걸어
      tools+system 프리픽스를 호출 간 재사용한다(비용 절감, 5분 TTL). 산출 불변.
    - **LangSmith 트래킹(선택)**: `LANGCHAIN_API_KEY`/`LANGSMITH_API_KEY`가 있으면
      `wrap_anthropic`로 감싼다(없으면 무트래킹으로 정상 동작).
    - anthropic·langsmith는 **이 함수 안에서만 지연 import**(P1 경계 + optional extra).
      ANTHROPIC_API_KEY가 없으면 RuntimeError(무키 방어).
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY 미설정 — annotate에는 API 키가 필요합니다."
        )
    import anthropic  # 지연 import

    client = anthropic.Anthropic(api_key=key)
    if os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY"):
        try:  # LangSmith 트레이싱(env로 on/off, 실패해도 주석은 계속)
            from langsmith.wrappers import wrap_anthropic

            client = wrap_anthropic(client)
        except Exception as e:  # noqa: BLE001
            print(f"[annotate] LangSmith 래핑 실패(무트래킹 진행): {e}", file=sys.stderr)

    def _call(*, system: str, user: str, schema: dict) -> dict | str:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=TEMPERATURE,
            # 프롬프트 캐싱: 상수인 system 프롬프트에 cache_control(ephemeral)을 걸어
            # tools+system 프리픽스를 호출 간 재사용한다(5분 TTL). user(시트별 layout)는
            # 매번 달라 캐시 불가. 모델이 보는 내용(토큰)은 동일하므로 산출·annotation_key
            # 불변 — 버전 범프 불필요.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[{
                "name": tool_name,
                "description": "요청된 스키마에 정확히 맞는 결과 하나를 방출한다.",
                "input_schema": schema,
            }],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user}],
        )
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                return b.input  # 스키마-유효 dict
        # 이례적으로 tool_use가 없으면 텍스트 폴백(하위 검증에서 걸림)
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )

    return _call


# ── 응답 파싱·검증 ────────────────────────────────────────────
def _extract_json(text: str) -> dict:
    """응답 텍스트에서 JSON 객체를 뽑는다(코드펜스 ```json 허용)."""
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return json.loads(t.strip())


class _EvidenceError(Exception):
    """스키마는 통과했으나 V2 실재성(주소가 used range 밖 등)에 실패한 응답."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class _ContextOverflow(Exception):
    """프롬프트가 모델 컨텍스트를 초과(too long). 축소 재시도/시트 제외의 트리거."""


def _is_context_overflow(e: Exception) -> bool:
    """**컨텍스트 길이 초과(프롬프트 과대)만** True. 그 외 4xx/오류는 False(정상 실패).

    anthropic 타입을 import하지 않고(P1 경계) 메시지로 판별한다 — 예: "prompt is too
    long: N tokens > 200000 maximum". 다른 BadRequestError는 초과가 아니므로 그대로 터진다.
    """
    msg = str(e).lower()
    return (
        "prompt is too long" in msg
        or "too many tokens" in msg
        or "maximum context" in msg
        or "context length" in msg
    )


def _evidence_problems(partial: dict, meta: dict) -> list[str]:
    """부분 semantics의 evidence 실재성 문제 목록(docx 등 미구현 형식은 생략)."""
    try:
        return collect_evidence_problems(partial, meta)
    except NotImplementedError:
        return []


def _call_unit(
    client, system: str, user: str, subschema: dict, *, label: str, eprint, validator=None
):
    """단위 1개 호출→파싱→하위 스키마 검증→(validator면) V2 실재성 검증.

    JSON/스키마/실재성 중 어느 하나라도 실패하면 오류를 첨부해 1회 재시도하고,
    재실패면 그 단위를 제외(None). validator(doc)는 실재성 문제 목록을 돌려준다.
    """
    attempt_user = user
    last_err: Exception | None = None
    for attempt in range(_MAX_RETRY + 1):
        # structured output이면 dict를 그대로, 텍스트 폴백/스텁 문자열이면 파싱한다.
        try:
            raw = client(system=system, user=attempt_user, schema=subschema)
        except Exception as e:  # 컨텍스트 초과만 구분해 상위로(축소 재시도용). 그 외는 그대로.
            if _is_context_overflow(e):
                raise _ContextOverflow(str(e)) from e
            raise
        try:
            doc = raw if isinstance(raw, dict) else _extract_json(raw)
            jsonschema.validate(doc, subschema)
            if validator is not None:
                problems = validator(doc)
                if problems:
                    raise _EvidenceError(problems)
            return doc
        except (json.JSONDecodeError, jsonschema.ValidationError, _EvidenceError) as e:
            last_err = e
            if attempt < _MAX_RETRY:
                hint = (
                    "evidence 주소가 실존하지 않거나 used range 밖입니다"
                    if isinstance(e, _EvidenceError)
                    else "JSON/스키마 검증에 실패했습니다"
                )
                attempt_user = (
                    user
                    + f"\n\n[재시도] 직전 응답이 {hint}: "
                    + str(e)
                    + "\n설명 없이, 실재하는 주소만 근거로 스키마에 맞는 JSON 객체 "
                    "하나만 다시 출력하세요."
                )
    eprint(f"[annotate] 단위 제외: {label} — {type(last_err).__name__}: {last_err}")
    return None


# ── 입력 조립 ─────────────────────────────────────────────────
def _layout_for_sheet(pkg: Path, name: str) -> str:
    """layout/*.html 중 data-sheet 마커가 이 시트인 파일 본문(없으면 빈 문자열)."""
    import html as _html

    marker = f'data-sheet="{_html.escape(name, quote=True)}"'
    for f in sorted((pkg / "layout").glob("*.html")):
        txt = f.read_text(encoding="utf-8")
        if marker in txt:
            return txt
    return ""


_EXCERPT_MARKER = '<tr class="excerpt"><td>(가운데 {n}행 생략 — layout 일부 발췌)</td></tr>'


def _excerpt_layout(layout: str, budget: int) -> tuple[str, bool]:
    """layout HTML을 char 예산 이하로 **행 경계** 발췌한다. 반환 (html, excerpted).

    §4.3 layout은 한 행 = 한 `<tr>…</tr>` 라인이다. 앞부분(head)+뒷부분(tail) 행을
    살리고 가운데를 생략 마커 한 줄로 대체한다 — `<tr>` 라인 단위로만 자르므로 행 중간이
    깨지지 않는다. 예산 이하면 원본 그대로(excerpted=False). head는 최소 1행 보장.
    """
    if len(layout) <= budget:
        return layout, False
    lines = layout.split("\n")
    row_pos = [i for i, ln in enumerate(lines) if ln.startswith("<tr")]
    if not row_pos:  # 이례적: 테이블 행 없음 — 하드 컷 폴백
        return layout[:budget], True
    first, last = row_pos[0], row_pos[-1]
    prefix, rows, suffix = lines[:first], lines[first : last + 1], lines[last + 1 :]
    overhead = len("\n".join(prefix)) + len("\n".join(suffix)) + 120  # 마커 여유
    rows_budget = max(budget - overhead, 0)
    head_budget = int(rows_budget * 0.7)
    head, used = [], 0
    for ln in rows:
        if head and used + len(ln) + 1 > head_budget:
            break  # head 비어 있으면 무조건 담아 최소 1행 보장
        head.append(ln)
        used += len(ln) + 1
    tail, used_t = [], 0
    for ln in reversed(rows[len(head) :]):
        if used_t + len(ln) + 1 > rows_budget - used:
            break
        tail.append(ln)
        used_t += len(ln) + 1
    tail.reverse()
    omitted = len(rows) - len(head) - len(tail)
    marker = [_EXCERPT_MARKER.format(n=omitted)] if omitted > 0 else []
    return "\n".join(prefix + head + marker + tail + suffix), True


def _cells_for_sheet(pkg: Path, name: str) -> list[str]:
    """이 시트에 속한 원장 줄(원문 jsonl)을 상한까지 발췌."""
    out: list[str] = []
    f = pkg / "data/cells.jsonl"
    with f.open(encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if obj.get("sheet") == name:
                out.append(s)
                if len(out) >= _CELL_CAP:
                    break
    return out


def _edges_for_sheet(refs: dict, name: str) -> list[dict]:
    pre = name + "!"
    return [
        e
        for e in refs.get("edges", [])
        if str(e.get("from", "")).startswith(pre) or str(e.get("to", "")).startswith(pre)
    ]


def _sheet_user_message(
    name: str, dims: str, layout: str, cells: list[str], edges: list[dict],
    *, excerpted: bool = False,
) -> str:
    edge_lines = "\n".join(f'  {e["from"]} → {e["to"]} ({e["ref_type"]})' for e in edges[:50])
    layout_note = (
        "\n※ 이 layout은 크기 제한으로 **일부 행만 발췌**됐습니다(가운데 생략, `(…행 생략)`"
        " 마커). 발췌에 나타나지 않은 영역은 관찰한 것처럼 주장하지 마세요."
        if excerpted else ""
    )
    return (
        f"# 요청: 시트 단위 주석\n"
        f"시트명: {name}\nused range: {dims}\n\n"
        f"## 레이아웃(HTML){layout_note}\n{layout}\n\n"
        f"## 원장(cells, jsonl {len(cells)}줄{' 이하 발췌' if len(cells) >= _CELL_CAP else ''})\n"
        + "\n".join(cells)
        + f"\n\n## 참조 엣지(이 시트 관련 {len(edges)}건)\n{edge_lines or '  (없음)'}\n\n"
        f"위 시트 하나에 대한 sheets[] 항목 JSON 객체 하나만 출력하세요. "
        f'evidence 주소는 반드시 "{name}!..." 형식입니다.'
    )


def _workbook_user_message(meta: dict, sheets_out: list[dict]) -> str:
    lines = []
    for s in sheets_out:
        lines.append(f'  - {s.get("name")}: {s.get("purpose", "")}')
    names = ", ".join(s["name"] for s in meta.get("sheets", []))
    return (
        f"# 요청: 워크북 단위 주석\n"
        f"파일: {meta.get('source', {}).get('filename', '')}\n"
        f"시트 목록: {names}\n\n"
        f"## 시트별 요지(직전 단계 산출)\n" + ("\n".join(lines) or "  (없음)") + "\n\n"
        f"워크북 전체를 관통하는 주장을 workbook_claims로 정리해 JSON 객체 하나만 "
        f"출력하세요. 각 evidence는 시트!주소 형식이며, 주장할 게 없으면 빈 배열로 두세요."
    )


def _call_sheet_unit(
    client, system: str, pkg: Path, meta: dict, refs: dict, s: dict, subschema: dict,
    *, eprint,
):
    """시트 1개 주석 — layout 예산 발췌 + 컨텍스트 초과 시 축소 1회 재시도, 최종 초과면 None.

    layout을 `_LAYOUT_BUDGET`으로 발췌해 호출하고, 그래도 프롬프트가 컨텍스트를 넘으면
    `_LAYOUT_BUDGET_MIN`으로 한 번 더 줄여 재시도한다. 그래도 초과면 그 시트만 제외(None).
    스키마/실재성 실패의 1회 재시도·제외는 `_call_unit`이 그대로 담당한다.
    """
    name = s["name"]
    full_layout = _layout_for_sheet(pkg, name)
    cells = _cells_for_sheet(pkg, name)
    edges = _edges_for_sheet(refs, name)
    for budget in (_LAYOUT_BUDGET, _LAYOUT_BUDGET_MIN):
        layout, excerpted = _excerpt_layout(full_layout, budget)
        user = _sheet_user_message(
            name, s.get("dimensions", ""), layout, cells, edges, excerpted=excerpted
        )
        try:
            return _call_unit(
                client, system, user, subschema, label=f"sheet {name}", eprint=eprint,
                validator=lambda d: _evidence_problems({"sheets": [d]}, meta),
            )
        except _ContextOverflow as e:
            if budget != _LAYOUT_BUDGET_MIN:
                eprint(f"[annotate] sheet {name}: 컨텍스트 초과 → layout 예산 축소 재시도")
                continue
            eprint(f"[annotate] 단위 제외: sheet {name} — 컨텍스트 초과(축소 후에도): {e}")
            return None


# ── 오케스트레이션 ────────────────────────────────────────────
def annotate_package(
    pkg, *, model: str | None = None, client=None, eprint=None, force: bool = False
) -> dict:
    """패키지에 data/semantics.json(draft)을 생성한다.

    client를 주입하면 그걸 쓰고(테스트·미리보기), 없으면 실 anthropic 클라이언트를
    만든다(ANTHROPIC_API_KEY 필요). 반환: {"path","sheets","excluded","cached"}.

    주석 캐시(§6): annotation_key(sha+annotator_version+model+prompt_sha)가 색인 항목과
    같고 semantics.json이 이미 있으면 **재주석을 생략**한다(LLM 미호출·클라이언트 미생성).
    force=True면 캐시를 무시하고 재생성한다.
    """
    pkg = Path(pkg)
    eprint = eprint or (lambda *a: print(*a, file=sys.stderr))
    model = model or DEFAULT_MODEL
    prompt_text, prompt_sha = _prompt_text_and_sha()

    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    out = pkg / "data" / "semantics.json"
    key = cache.annotation_key(
        meta["source"]["sha256"], ANNOTATOR_VERSION, model, prompt_sha
    )
    root, dirname = pkg.parent, pkg.name
    sem_schema = _load_schema("semantics.schema.json")

    # 주석 캐시 hit: 완료 marker를 approve·verify·승계와 **같은 기준**으로 보고, 나아가
    # 저장된 본문이 지금도 계약(스키마 V1 + V2 실재성)을 만족할 때만 재주석을 생략한다.
    #  ① 키 3자 일치: _index.annotation_key == meta.annotation.annotation_key ==
    #     semantics.generator 재계산 키 == 현재 실행 key.
    #  ② 본문 유효: 저장된 semantics가 전체 스키마와 V2(주소 실재성)를 통과.
    # 어느 하나라도 어긋나면(훼손·부분·stale·본문 위반) miss로 보고 재주석한다 —
    # "annotate 산출물은 항상 verify를 통과한다"는 계약을 캐시 경로에서도 지킨다.
    # 클라이언트는 hit이 아닐 때만 만든다.
    if not force and out.is_file():
        entry = cache.load_index(root)["entries"].get(dirname)
        idx_key = entry.get("annotation_key") if entry else None
        meta_key = meta.get("annotation", {}).get("annotation_key")
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        gen = (existing or {}).get("generator", {})
        expected = cache.annotation_key(
            meta.get("source", {}).get("sha256", ""),
            gen.get("annotator_version", ""),
            gen.get("model", ""),
            gen.get("prompt_sha", ""),
        )
        body_ok = False
        if existing is not None and key == idx_key == meta_key == expected:
            try:  # ② 본문이 지금도 스키마 V1 + V2 실재성을 통과하는가
                jsonschema.validate(existing, sem_schema)
                body_ok = not _evidence_problems(existing, meta)
            except jsonschema.ValidationError:
                body_ok = False
        if body_ok:
            eprint(f"[annotate cache hit] {pkg.name} → 재주석 생략")
            return {
                "path": out,
                "sheets": len(existing.get("sheets", [])),
                "excluded": [],
                "cached": True,
            }

    if client is None:
        client = build_anthropic_client(model)

    refs = json.loads((pkg / "data/references.json").read_text(encoding="utf-8"))
    sheet_subschema = sem_schema["properties"]["sheets"]["items"]
    wb_subschema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["workbook_claims"],
        "properties": {"workbook_claims": sem_schema["properties"]["workbook_claims"]},
    }

    sheets_out: list[dict] = []
    excluded: list[str] = []
    for s in meta.get("sheets", []):
        doc = _call_sheet_unit(
            client, prompt_text, pkg, meta, refs, s, sheet_subschema, eprint=eprint,
        )
        if doc is None:
            excluded.append(s["name"])
        else:
            sheets_out.append(doc)

    wb_doc = _call_unit(
        client, prompt_text, _workbook_user_message(meta, sheets_out), wb_subschema,
        label="workbook_claims", eprint=eprint,
        validator=lambda d: _evidence_problems(
            {"workbook_claims": d.get("workbook_claims", [])}, meta
        ),
    )
    if wb_doc is None:
        excluded.append("workbook_claims")
    workbook_claims = wb_doc["workbook_claims"] if wb_doc else []

    semantics = {
        "generator": {
            "model": model,
            "annotator_version": ANNOTATOR_VERSION,
            "prompt_sha": prompt_sha,
            "temperature": TEMPERATURE,
            "generated_at": _now_iso(),
        },
        "review": {"status": "draft", "reviewed_at": None, "note": None},
        "workbook_claims": workbook_claims,
        "sheets": sheets_out,
    }
    # 최종 sanity: 전체 스키마 + V2 실재성(단위 검증을 통과한 조각들의 조립이라 정상
    # 통과 기대. 만에 하나 잔존하면 경고로 남긴다 — 산출은 이미 단위별로 V2-clean).
    jsonschema.validate(semantics, sem_schema)
    sanity = _evidence_problems(semantics, meta)
    if sanity:
        eprint(f"[annotate] 경고: 최종 V2 잔존 {len(sanity)}건: {sanity[:5]}")

    out.write_text(
        json.dumps(semantics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # 완료 marker: excluded 없는 완료 주석만 annotation_key를 남긴다. 부분 실패면 None으로
    # (캐시 오염·partial 승인 방지). meta.annotation.annotation_key(패키지-독립 marker)와
    # _index.annotation_key(캐시/승계 미러) 둘 다 같은 값으로 세운다.
    key_to_record = None if excluded else key
    set_annotation(
        pkg,
        present=True,
        annotator_version=ANNOTATOR_VERSION,
        review_status="draft",
        annotation_key=key_to_record,
    )
    if cache.update_annotation(
        root, dirname, annotation_key=key_to_record, review_status="draft"
    ) is None:
        eprint(f"[annotate] 경고: _index.json에 {dirname} 항목 없음 — 주석 캐시 미기록")
    return {"path": out, "sheets": len(sheets_out), "excluded": excluded, "cached": False}
