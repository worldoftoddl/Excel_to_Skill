# excel-to-skill

회계감사조서 Excel(`.xlsx`·`.xls`)을 빠르게 이해하고 질의할 수 있도록 workbook 사실,
감사·회계기준 문맥, agent-ready brief로 준비하고 근거 기반 브리핑·질의응답까지 수행하는
CLI 도구다. 일반 spreadsheet 구조 조회도 기존 명령으로 유지한다.

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

# LLM 호출 전 전체·시트별 region 수와 예상 호출량 확인(외부 호출 없음)
excel-to-skill audit-scopes <패키지>

# 감사조서 준비 — 기본은 workbook 전체를 한 번에 분석
excel-to-skill prepare <패키지> [--scope workbook] [--mcp-config .mcp.json] [--force]

# 필요한 시트만 각각 독립 분석하거나, 내용이 있는 전 시트를 각각 분석
excel-to-skill prepare <패키지> --sheet C --sheet P
excel-to-skill prepare <패키지> --all-sheets

# 감사 준비본 사람 검토 — facts와 brief를 함께 승인/반려
excel-to-skill audit-review <패키지> --approve
excel-to-skill audit-review <패키지> --reject --note "<반려 사유>"
excel-to-skill audit-review <패키지> --sheet C --approve

# 소비(Agent) — 원본 JSON 통째 로드 금지, 개요→시트→셀 단계 조회
excel-to-skill overview <패키지> [--sheet <시트>]         # 셀 원문 없이 구조·상태 요약
excel-to-skill inspect  <패키지> --sheet <시트> [--range A1:B10 | --cell A1]
excel-to-skill search   <패키지> --query <문자열> [--sheet <시트>]
excel-to-skill refs     <패키지> --cell <시트!A1>

# 감사조서 준비본 소비(audit-rag-v0)
excel-to-skill brief       <패키지> [--sheet C] [--limit 100]
excel-to-skill audit-search <패키지> --query <문자열> [--kind <종류>] [--sheet C]
excel-to-skill audit-get    <패키지> --id <fact/statement/citation ID> [--sheet C]
excel-to-skill assertion-procedures <패키지> [--query <문자열>] [--sheet C]
excel-to-skill trace        <패키지> --id <ID> [--sheet C] [--limit 100]

# 브리핑 에이전트 — 질문 생략 시 전체 브리핑, --json이면 구조화 근거 포함
excel-to-skill audit-agent <패키지> [--sheet C] [--question <질문>] [--json]
```

소비 명령은 결정론(키 불요)이며 결과 JSON을 stdout으로 낸다. 출력은 하드 상한(`--limit`)과
`returned`·`total`·`truncated`로 예산이 걸리고, `overview`는 승인된 해석만 노출한다.
`assertion-procedures`의 상한은 상위 목록뿐 아니라 각 pair의 관계·결과·trace 목록에도
적용되며, 각 목록의 `returned_*`·`total_*`·`*_truncated` 필드로 잘린 범위를 밝힌다.
`audit-search`는 의미 임베딩 검색이 아니라 대소문자를 무시한 텍스트 부분일치 검색이다.

`convert`의 stdout은 패키지 경로만, `annotate`의 stdout은 산출 `semantics.json` 경로만,
`review`의 stdout은 재생성된 `SKILL.md` 경로만 출력한다(진행·경고·오류는 stderr, 판정은
exit code). `annotate --force`는 주석 캐시를 무시하고 재주석한다.

## 감사조서 준비 계층(audit-rag-v0)

기존 `semantics.json`은 M3 비교 기준선으로 유지한다. 새 감사조서 경로는 서로 다른
출처가 섞이지 않도록 세 산출물로 분리한다.

- `data/audit_facts.json`: workbook에 실제로 문서화된 감사 사실과 실제 셀 원장 digest
- `data/standards_context.json`: 사실에서 도출한 query로 조회한 감사·회계기준 문맥
- `data/audit_brief.json`: workbook fact ID와 기준서 citation ID를 분리 인용하는 준비본

### 큰 workbook의 분석 범위

`convert`는 선택과 무관하게 먼저 workbook 전체를 기계적으로 파싱한다. 따라서 시트별
LLM 분석을 선택해도 `cells.jsonl`, 수식 참조, 진단정보와 시트 목록을 물리적으로 쪼개거나
잃지 않는다. 선택은 그 다음 의미 분석 단계에만 적용된다.

- 무옵션 `prepare` 또는 `--scope workbook`: 기존처럼 모든 시트를 하나의 audit bundle로 분석
- 반복 `--sheet`: 지정한 각 시트를 별도 bundle로 분석. 시트 간 fact ID namespace도 분리
- `--all-sheets`: ledger cell이 있는 모든 시트를 각각 분석. 한 시트 실패가 이미 커밋된 다른
  시트 결과를 되돌리지 않음
- `audit-scopes`: 시트별 dimensions·cell·region·직접 수식 dependency·예상 LLM 호출량과
  준비/검토 상태를 외부 호출 없이 표시

시트 scope의 모델 입력에는 선택한 시트 region만 들어간다. 수식 자체에 다른 시트 참조가
있으면 그 참조 표현은 보이지만, 참조 대상 시트의 셀 내용까지 자동으로 합치지는 않는다.
필요하면 dependency 정보를 보고 그 시트를 별도 `--sheet`로 준비한다.

기존 workbook bundle은 고정 `data/audit_*.json`과 `meta.audit_preparation`을 유지한다. 시트
bundle은 원 시트명을 경로에 쓰지 않고
`data/audit_scopes/sheets/<sheet-name-sha256>/` 아래에 저장하며, 세 artifact 게시 뒤
`commit.json`을 마지막에 기록한다. 커밋이 없거나 digest가 맞지 않는 scope는 모든 reader와
agent가 거부하고 workbook 결과로 fallback하지 않는다. 시트 prepare/review는 root
`meta.json`·`SKILL.md`와 다른 시트 scope를 변경하지 않는다. 시트별 조회·검토·agent에는
항상 같은 `--sheet`를 지정한다.

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
relation trace는 relation 레코드가 직접 인용한 `relation_direct_*` source/cell과 양 endpoint
fact의 `endpoint_*` source/cell을 분리한다. 두 범위를 합친 `sources`·`cells`도 호환용으로
남지만, 관계 자체의 직접 근거를 볼 때는 `relation_direct_*`를 사용한다.

현재 brief 계약은 `audit_brief.v2`다. v1 준비본은 최신 계약으로 오인하지 않고 소비 단계에서
거부되며, `prepare`를 다시 실행하면 검증된 facts·standards cache를 재사용해 brief만 v2로
갱신할 수 있다.

### 브리핑 에이전트

`audit-agent`는 준비된 세 artifact에 새 사실을 쓰지 않는 일회성 read-only 에이전트다.
항상 commit marker와 artifact digest 검증을 먼저 통과한 뒤 `brief`와
`assertion-procedures`를 읽고, 필요할 때만 `audit-search`·`audit-get`·`trace`를 제한적으로
호출한다. 실행 시에는 `ANTHROPIC_API_KEY`가 필요하지만 이미 저장된 기준서 citation만
사용하므로 MCP를 다시 호출하지 않는다.

모델은 최종 문장을 직접 쓰지 않고 관찰된 `statement`·`fact`·`relation`·
`standard_citation` ID만 선택한다. 코드가 선택된 원본 record의 문장·상태·신뢰도를 그대로
가져오고, workbook 셀과 검증 기준서 CID를 `trace`로 보강한다. 자유 텍스트나 셀 값에 우연히
등장한 ID는 선택 권한으로 승격하지 않는다. ID 기반 `audit-get`·`trace`도 앞선 typed
결과에서 관찰된 ID만 허용한다. facts와 brief의 검토상태, 새 답변의 검토상태를 각각
분리하며, 둘 중 하나가 rejected이면 모델을 호출하지 않는다. 사람 검토 후 `audit-review`로
facts와 brief를 함께 승인/반려할 수 있고, 새 답변은 원본 bundle이 approved여도 항상
`unreviewed`다.

도구 결과가 잘리거나 trace가 불완전하면 `coverage.complete=false`와 limitation을 표시한다.
각 turn의 직렬화된 관찰 payload는 600KB로 제한되고 동일 도구 요청은 반복할 수 없다.
`rejected` 또는
`not_ready` 입력은 외부 모델을 호출하지 않고 결정론적으로 답변을 보류한다. `--json` 출력은
`audit_agent_response.v2` 스키마로 재검증되며, ID namespace를 고정하는 workbook 또는
sheet scope identity가 필수로 포함된다.

기준서가 선택된 claim은 collection이 붙은 검증 CID와 최대 400자의 원문 발췌를 함께
표시한다. brief 문장이 `KSA`/`KIFRS` 번호를 직접 쓸 때는 같은 문장의 citation이 그
기준서에 속해야 한다. 그렇지 않으면 문장을 그럴듯하게 고치지 않고 통째로 제외하며,
제외한 statement ID를 readiness 사유에 남긴다.

`audit-agent`는 brief statement, fact 설명, relation과 기준서 요약을 모델 제공자에게
전송한다. 모델이 `trace`를 선택하면 해당 범위의 원문 셀 값·수식도 다음 turn에 포함될 수
있으며, `--json` 출력에도 선택된 raw cell이 담긴다. LangSmith 키가 활성화되어 있으면 같은
호출이 외부 trace에 기록될 수 있다. 민감 조서는 승인된 처리환경에서만 실행하고, 추적을
끄려면 `LANGCHAIN_API_KEY= LANGSMITH_API_KEY= excel-to-skill audit-agent ...`처럼 두 변수를
빈 값으로 명시해 `.env` 자동 로드를 덮어쓴다.

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
이미 게시된 facts와 standards context가 현재 모델·조회정책·artifact key에 맞고 brief만
갱신하면 되는 경우, `prepare`는 MCP 연결이나 토큰 없이 두 상류 단계를 검증·재사용한다.
조회정책·collection·상류 artifact가 달라졌거나 `--force`를 사용하면 정상적으로 MCP
연결을 요구한다.

## 어노테이터(해석 계층)

- **모델**: 기본값은 **`claude-sonnet-4-5`**. `annotate --model <이름>`으로 교체한다.
- **키**: `ANTHROPIC_API_KEY` 환경변수가 필요하다. 없으면 `annotate`·`prepare`·
  `audit-agent`는 명확히 실패하며, `convert`·`verify`와 결정론 소비 명령은 영향받지 않는다
  (anthropic import는 지연된다).
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
