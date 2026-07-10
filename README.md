# excel-to-skill

스프레드시트(`.xlsx`·`.xls`)와 워드 문서(`.docx` — M4 예정)를 읽어, AI 에이전트가
**근거(주소) 추적 가능하게** 소비할 수 있는 스킬 패키지로 변환하는 독립 CLI 도구.

산출 패키지는 결정론 계층(코드만으로 추출)과 해석 계층(LLM 주석)을 분리하며, 모든 의미
주장은 원본 셀 주소를 근거로 동반한다.

## 설치

```bash
uv sync                    # 결정론 계층(convert·verify)
uv sync --extra annotate   # 해석 계층(annotate)까지 — anthropic 포함
```

## 명령

```bash
# 결정론 변환(해석 계층 없이) — 패키지 폴더 생성
excel-to-skill convert <파일.xlsx> [--out ./converted] [--max-rows 5000] [--full-names]
excel-to-skill convert <디렉터리> --all

# 패키지 계약 검증 — V1 스키마 / V2 evidence 실재성 / V3 재현성(--source)
excel-to-skill verify <패키지> [--source <원본>]

# 해석 계층(semantics.json draft) 생성 — 기존 패키지에 주석 추가
excel-to-skill annotate <패키지> [--model <모델명>] [--force]

# 해석 계층 검토 — 승인(승인판 SKILL.md 재생성) / 반려(사유 필수)
excel-to-skill review <패키지> --approve
excel-to-skill review <패키지> --reject --note "<반려 사유>"
```

`convert`의 stdout은 패키지 경로만, `annotate`의 stdout은 산출 `semantics.json` 경로만,
`review`의 stdout은 재생성된 `SKILL.md` 경로만 출력한다(진행·경고·오류는 stderr, 판정은
exit code). `annotate --force`는 주석 캐시를 무시하고 재주석한다.

## 어노테이터(해석 계층)

- **모델**: 기본값은 **`claude-sonnet-4-5`**. `annotate --model <이름>`으로 교체한다.
- **키**: `ANTHROPIC_API_KEY` 환경변수가 필요하다. 없으면 `annotate`는 명확히 실패하며,
  `convert`·`verify`는 영향받지 않는다(anthropic 경계는 어노테이터 모듈에만 있다).
- **Structured output**: 응답 스키마를 도구로 강제(`tool_choice`)해 스키마-유효 JSON을
  구조적으로 방출받는다(텍스트 파싱 실패 제거). 그 위에 스키마 재검증 + V2 실재성 + 1회
  재시도가 걸린다.
- **LangSmith 트래킹(선택)**: `LANGCHAIN_API_KEY`(또는 `LANGSMITH_API_KEY`)가 있으면 각
  호출을 자동 트레이스한다(`LANGCHAIN_TRACING_V2`·`LANGCHAIN_PROJECT`로 제어). 없으면
  무트래킹으로 정상 동작. `langsmith`도 어노테이터 모듈 안에서만 지연 import(P1 경계).
- 생성 직후 상태는 `draft`다. 승인·승인판 SKILL.md 재생성(`review`)은 `review --approve`.
  승인 전 `verify`로 V2(evidence 실재성) 통과를 확인하길 권장한다.
