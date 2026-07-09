"""§7 어노테이터 — 해석 계층(data/semantics.json) 생성기.

**P1 물리적 경계: `anthropic`을 import하는 유일한 모듈이다.** 그것도
`build_anthropic_client()` 안에서만 지연 import하므로, 클라이언트를 주입해 쓰는 경로
(테스트·미리보기)는 anthropic 미설치·무네트워크에서도 돈다. convert/verify는 이 모듈을
아예 건드리지 않고, cli의 `annotate` 서브커맨드만 지연 import한다.

동작(§7):
  - 입력 = 패키지의 layout/*.html(구조) + data/cells.jsonl(주소 근거) + references(요약).
  - **시트 단위**로 호출해 sheets[] 항목을 만들고, 마지막에 **워크북 단위** 1회로
    workbook_claims를 만든다(호출 순서: meta.sheets 순 → workbook).
  - temperature 0, 응답은 JSON만. 받은 JSON을 semantics.schema.json의 해당 하위 스키마로
    검증하고, 불일치면 오류를 첨부해 **1회 재시도**, 재실패면 그 단위를 결과에서 **제외**
    하고 stderr로 보고한다(진단 파일에는 LLM 실패를 남기지 않는다).
  - 산출은 status="draft"인 semantics.json. 승인/재생성은 review 단계(별도).

클라이언트 계약: `client(*, system: str, user: str) -> str` (모델·temperature는 팩토리가
캡슐화). 반환 문자열이 곧 모델 응답 텍스트다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import jsonschema

from .meta import _now_iso

_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_PATH = _ROOT / "prompts" / "annotator_v1.md"
_SCHEMA_DIR = _ROOT / "schemas"

ANNOTATOR_VERSION = "0.1.0"
DEFAULT_MODEL = "claude-sonnet-5"  # §7 기본 모델명 — 유일 출처(코드 상수 1곳 + README)
TEMPERATURE = 0
_MAX_RETRY = 1  # 스키마 불일치 시 오류 첨부 1회 재시도
_MAX_TOKENS = 4096
_CELL_CAP = 400  # 시트당 프롬프트에 넣는 원장 줄 상한(발췌 — 구현 재량)


def _prompt_text_and_sha() -> tuple[str, str]:
    raw = _PROMPT_PATH.read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


# ── 실 클라이언트 팩토리 (anthropic import는 여기서만) ─────────────
def build_anthropic_client(model: str):
    """실 anthropic 클라이언트를 `(*, system, user) -> str` 콜러블로 감싼다.

    ANTHROPIC_API_KEY가 없으면 RuntimeError(무키 환경 방어). anthropic 패키지는 이
    함수 안에서만 import한다(P1 경계 + optional extra).
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY 미설정 — annotate에는 API 키가 필요합니다."
        )
    import anthropic  # 지연 import

    client = anthropic.Anthropic(api_key=key)

    def _call(*, system: str, user: str) -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            temperature=TEMPERATURE,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
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


def _call_unit(client, system: str, user: str, subschema: dict, *, label: str, eprint):
    """단위 1개 호출→파싱→하위 스키마 검증. 실패 시 1회 재시도, 재실패면 None."""
    attempt_user = user
    last_err: Exception | None = None
    for attempt in range(_MAX_RETRY + 1):
        text = client(system=system, user=attempt_user)
        try:
            doc = _extract_json(text)
            jsonschema.validate(doc, subschema)
            return doc
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            last_err = e
            if attempt < _MAX_RETRY:
                attempt_user = (
                    user
                    + "\n\n[재시도] 직전 응답이 JSON/스키마 검증에 실패했습니다: "
                    + str(e)
                    + "\n설명 없이 스키마에 맞는 JSON 객체 하나만 다시 출력하세요."
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


def _sheet_user_message(name: str, dims: str, layout: str, cells: list[str], edges: list[dict]) -> str:
    edge_lines = "\n".join(f'  {e["from"]} → {e["to"]} ({e["ref_type"]})' for e in edges[:50])
    return (
        f"# 요청: 시트 단위 주석\n"
        f"시트명: {name}\nused range: {dims}\n\n"
        f"## 레이아웃(HTML)\n{layout}\n\n"
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


# ── 오케스트레이션 ────────────────────────────────────────────
def annotate_package(pkg, *, model: str | None = None, client=None, eprint=None) -> dict:
    """패키지에 data/semantics.json(draft)을 생성한다.

    client를 주입하면 그걸 쓰고(테스트·미리보기), 없으면 실 anthropic 클라이언트를
    만든다(ANTHROPIC_API_KEY 필요). 반환: {"path", "sheets", "excluded"}.
    """
    pkg = Path(pkg)
    eprint = eprint or (lambda *a: print(*a, file=sys.stderr))
    model = model or DEFAULT_MODEL
    prompt_text, prompt_sha = _prompt_text_and_sha()
    if client is None:
        client = build_anthropic_client(model)

    meta = json.loads((pkg / "meta.json").read_text(encoding="utf-8"))
    refs = json.loads((pkg / "data/references.json").read_text(encoding="utf-8"))
    sem_schema = _load_schema("semantics.schema.json")
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
        name = s["name"]
        user = _sheet_user_message(
            name,
            s.get("dimensions", ""),
            _layout_for_sheet(pkg, name),
            _cells_for_sheet(pkg, name),
            _edges_for_sheet(refs, name),
        )
        doc = _call_unit(
            client, prompt_text, user, sheet_subschema,
            label=f"sheet {name}", eprint=eprint,
        )
        if doc is None:
            excluded.append(name)
        else:
            sheets_out.append(doc)

    wb_doc = _call_unit(
        client, prompt_text, _workbook_user_message(meta, sheets_out), wb_subschema,
        label="workbook_claims", eprint=eprint,
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
    # 최종 전체 스키마 sanity(하위 검증을 통과한 조각들의 조립이라 정상 통과 기대).
    jsonschema.validate(semantics, sem_schema)

    out = pkg / "data" / "semantics.json"
    out.write_text(
        json.dumps(semantics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"path": out, "sheets": len(sheets_out), "excluded": excluded}
