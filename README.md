# excel-to-skill

회계감사조서 Excel(`.xlsx`·`.xls`)을 빠르게 이해하고 질의할 수 있도록 workbook 사실,
감사·회계기준 문맥, agent-ready brief로 준비하는 CLI 도구다. 일반 spreadsheet 구조 조회도
기존 명령으로 유지한다.

셀 주소는 제품의 최종 목적이 아니라 **설명과 결론을 원문으로 되짚는 provenance 계층**이다.
산출 패키지는 workbook-only 사실, 별도 RAG 기준서 문맥, 둘을 구분 인용하는 brief를
분리해 에이전트가 무엇이 조서에 적힌 내용이고 무엇이 외부 권위 문맥인지 혼동하지 않게 한다.

## 설치

```bash
uv sync                    # 결정론 계층(convert·verify)
uv sync --extra annotate   # 해석 계층(annotate)까지 — anthropic 포함
uv sync --extra prepare    # 감사조서 prepare — anthropic + FastMCP HTTP client
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
excel-to-skill annotate <converted_root> --all   # 전 패키지 일괄 주석(집계·실패 격리)

# 해석 계층 검토 — 승인(승인판 SKILL.md 재생성) / 반려(사유 필수)
excel-to-skill review <패키지> --approve
excel-to-skill review <패키지> --reject --note "<반려 사유>"

# 감사조서 준비 — workbook facts → auditpaper 기준서 MCP → agent-ready brief
excel-to-skill prepare <패키지> [--mcp-config .mcp.json] [--model <모델명>] [--force]

# 소비(Agent) — 원본 JSON 통째 로드 금지, 개요→시트→셀 단계 조회
excel-to-skill overview <패키지> [--sheet <시트>]         # 셀 원문 없이 구조·상태 요약
excel-to-skill inspect  <패키지> --sheet <시트> [--range A1:B10 | --cell A1]
excel-to-skill search   <패키지> --query <문자열> [--sheet <시트>]
excel-to-skill refs     <패키지> --cell <시트!A1>

# 감사조서 준비본 소비(audit-rag-v0)
excel-to-skill brief       <패키지> [--limit 100]
excel-to-skill audit-search <패키지> --query <문자열> [--kind <종류>]
excel-to-skill audit-get    <패키지> --id <fact/statement/citation ID>
excel-to-skill assertion-procedures <패키지> [--query <문자열>] [--limit 100]
excel-to-skill trace        <패키지> --id <ID> [--limit 100]
```

소비 명령은 결정론(키 불요)이며 결과 JSON을 stdout으로 낸다. 출력은 하드 상한(`--limit`)과
`returned`·`total`·`truncated`로 예산이 걸리고, `overview`는 승인된 해석만 노출한다.
`assertion-procedures`의 상한은 상위 목록뿐 아니라 각 pair의 관계·결과·trace 목록에도
적용되며, 각 목록의 `returned_*`·`total_*`·`*_truncated` 필드로 잘린 범위를 밝힌다.

`convert`의 stdout은 패키지 경로만, `annotate`의 stdout은 산출 `semantics.json` 경로만,
`review`의 stdout은 재생성된 `SKILL.md` 경로만 출력한다(진행·경고·오류는 stderr, 판정은
exit code). `annotate --force`는 주석 캐시를 무시하고 재주석한다.

## 감사조서 준비 계층(audit-rag-v0)

기존 `semantics.json`은 M3 비교 기준선으로 유지한다. 새 감사조서 경로는 서로 다른
출처가 섞이지 않도록 세 산출물로 분리한다.

- `data/audit_facts.json`: workbook에 실제로 문서화된 감사 사실과 실제 셀 원장 digest
- `data/standards_context.json`: 사실에서 도출한 query로 조회한 감사·회계기준 문맥
- `data/audit_brief.json`: workbook fact ID와 기준서 citation ID를 분리 인용하는 준비본

조서에 기준서 번호가 명시되면 query의 `standard_nos`로 구조화해 MCP filter에 그대로
전달한다. 현재 citation 유형은 감사기준·회계기준만 구분하므로 `GUIDE` 실무지침은 잘못된
유형으로 기록하지 않고 명시적으로 조회 제한으로 남긴다.

추출은 시트 첫 N개 셀을 자르는 대신 모든 `cells.jsonl` 레코드를 결정론적 region으로
나눠 처리한다. workbook 주소는 used range 포함 여부만 보지 않고 실제 원장 레코드에
연결되는지와 내용 SHA-256까지 검증한다. brief의 draft 내용은 숨기지 않으며
`unreviewed: true`로 명시해 탐색에 사용하고, `trace`로 원문과 기준서 문맥을 확인한다.
가변 brief 본문은 `SKILL.md`에 복제하지 않는다. SKILL은 안정적인 bootstrap만 제공하고,
에이전트는 commit marker와 artifact digest를 검증하는 `excel-to-skill brief`로 내용을
불러온다.

`assertion-procedures`는 텍스트 유사도로 대응 관계를 추정하지 않는다. 준비본에
`procedure --tests--> assertion` 방향으로 명시된 관계만 주장·절차 쌍으로 반환하고,
그 절차에서 `produces`로 직접 연결된 result/finding만 함께 보여준다. 관계가 없는 주장과
절차는 각각 미연결 목록으로 남겨 누락을 숨기지 않는다. 산출물에 `inferred`로 기록된
관계도 숨기지 않되 pair의 `mapping_status`와 상태별 개수로 `documented` 관계와 구분한다.

Python 오케스트레이션 진입점은
`excel_to_skill.audit.prepare.prepare_package(...)`이다. 모델 client와
`StandardsRetriever`를 주입하므로 단위 테스트는 무네트워크로 실행된다. CLI는
`auditpaper-standards`의 Streamable HTTP MCP를 사용한다. `.mcp.json` 형식은 다음과 같다.

```json
{
  "mcpServers": {
    "auditpaper-standards": {
      "type": "http",
      "url": "https://<서버>/mcp",
      "headers": {"Authorization": "Bearer ${MCP_AUTH_TOKEN}"}
    }
  }
}
```

토큰은 현재 작업 디렉터리의 ignored `.env`에 `MCP_AUTH_TOKEN=...`으로 두거나 이미
export된 환경변수로 주입하고 저장소에 커밋하지 않는다. `prepare`와 `annotate`는 `.env`를
자동 로드하되 기존 export 값을 덮어쓰지 않는다. URL은
`--mcp-url` 또는 `AUDITPAPER_MCP_URL`로도 줄 수 있다. URL/config를 생략하면 원격 MCP
안내서의 고정 HF Space(`https://toddl-auditpaper-mcp.hf.space/mcp`)를 사용한다.
prepare는 서버의 `collection`을 cache 판본으로 고정하고, 검색으로 채택한 모든 CID를
`standards_get_paragraph` 직조회 또는 같은 `collection + cid`의 검증 cache로 확정한다.
서버가 구조화 시행일을 제공하지 않으므로 적용일 적합성은
자동 단정하지 않고 brief limitation으로 남긴다. 검증한 전문은
`<패키지 상위>/.auditpaper_standards_cache/`에 `collection + cid` 기준으로 원문 그대로
원자 저장하며, collection이 바뀌면 자동으로 다른 cache namespace를 사용한다.

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
- **큰 시트 입력 예산**: 시트 단위 프롬프트의 layout HTML이 크면 **행 경계로 발췌**(앞+뒤
  보존, 가운데 생략 표시)해 모델 컨텍스트를 넘지 않게 한다. 그래도 초과하면 예산을 줄여
  1회 재시도하고, 최종 초과 시 그 시트만 제외한다(나머지 시트·워크북은 계속). 입력 정책이
  바뀌면 `ANNOTATOR_VERSION`이 올라 기존 주석 캐시가 무효화된다.
- 생성 직후 상태는 `draft`다. 승인·승인판 SKILL.md 재생성(`review`)은 `review --approve`.
  승인 전 `verify`로 V2(evidence 실재성) 통과를 확인하길 권장한다.
