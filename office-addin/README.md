# Audit Workbook Office Add-in

이 디렉터리는 Python workbook-edit backend가 발행한 승인 manifest를 실제 Excel 세션에
적용하는 Office.js task pane이다. 모델이 JavaScript를 실행하는 도구가 아니며, 다음 고정
순서만 수행한다.

```text
workflow 조회 → 현재 셀 재조회 → exact preview → 사용자 승인
→ one-use claim/fence → write-start 확인 → before 재조회
→ immutable manifest 적용 → 필요 시 worksheet 재계산
→ after 재조회 → backend 검증 → 선택적 Workbook.save()
```

`session_verified`는 현재 Excel 세션의 authored state 재조회가 승인 상태와 일치했다는
뜻이다. 파일 bytes가 영속 저장됐거나 새 audit snapshot이 만들어졌다는 뜻은 아니다.

## 로컬 검증

Vite 8을 실행할 수 있는 Node.js 20.19+ 또는 22.12+ 환경이 필요하다.

```bash
cd office-addin
npm ci
npm test
npm run build
npm run validate:manifest
npm run dev
```

`npm run dev`는 개발 인증서를 준비하고 `https://localhost:3000`에서 task pane을 제공한다.
최초 실행은 로컬 인증기관을 OS trust store에 등록하기 위한 확인이나 관리자 권한을 요구할 수
있다. 그 다음 사용하는 Excel host의 공식 sideload 절차로 `manifest.xml`을 등록한다. manifest와
Content-Security-Policy는 localhost 개발 harness용이다.

이 명령은 Python backend를 시작하거나 Excel에 Add-in을 자동 등록하지 않는다. Excel 웹에서
확인할 때는 같은 머신의 브라우저에서 `https://localhost:3000/taskpane.html`을 먼저 열어 인증서를
확인한 뒤 `Home > Add-ins > More Settings > Upload My Add-in`에서 `manifest.xml`을 선택한다.
다른 PC에서 연 Excel의 `localhost`는 개발 서버가 아니라 그 PC 자신을 가리킨다. 현재 package에는
desktop 자동 sideload용 `office-addin-debugging`/`start`/`stop` script가 없다. 자세한 절차는
[Microsoft의 수동 sideload 안내](https://learn.microsoft.com/en-us/office/dev/add-ins/testing/sideload-office-add-ins-for-testing)를
따른다. 개발 CA를 더 이상 쓰지 않으면 `npx office-addin-dev-certs uninstall`로 제거할 수 있다.
`npm run validate:manifest`는 Microsoft 검증 서비스 연결이 필요하다.

## Host 연결 계약

개발 화면에서는 이미 backend에 만들어진 workflow의 API URL과 ID를 수동 입력할 수 있다.
proposal 생성이나 Office session 등록 UI는 포함하지 않는다. 배포 host는 task pane 모듈이
로드되기 전에 같은 origin의 인증된 bootstrap script로 값을 고정해야 한다.

```ts
window.auditWorkbookEditHost = {
  apiBaseUrl: "https://<host-owned-origin>",
  workflowId: "<opaque-workflow-id>",
  publishVerifiedSnapshot: async (verifiedSave) => {
    // 저장된 cloud asset을 server 측에서 다시 취득하고 hash/영속화한 뒤
    // 새 snapshot identity만 반환한다.
  },
};
```

운영 환경에서는 다음이 host 책임이다.

- 인증된 principal과 현재 workbook을 backend `WorkbookSessionBinding`에 등록
- 공동편집 세션에서는 같은, 복사본에서는 다른 stable `workbook_instance_id` 발급
- API origin/auth/CORS를 고정하고 개발용 자유 URL 및 `connect-src https:` 제거
- `session_verified` 뒤 cloud file 재취득, SHA-256 계산, immutable asset 저장, 새 snapshot 등록
- verify부터 asset 재취득까지 workbook-level publication lease 유지 또는 base revision CAS
- save/callback 응답 유실 시 execution ID 기반 publication-only 조회·재개
- `verification_failed`/`indeterminate` 및 pending claim 조정

현재 `HostCallbackSnapshotPublisher`는 callback의 반환값과 새 bundle revision 여부를
검증하는 integration contract일 뿐이다. asset 저장 API나 callback 주입 구현은 이 저장소에
포함돼 있지 않다. 불확실 오류가 표시되면 같은 편집을 다시 적용하지 말고 host 상태를 먼저
조정해야 한다.

Backend는 현재 `session_verified`에서 execution lock을 해제하므로, 그 뒤 단순히
`Workbook.save()`하고 파일을 재취득하면 사이에 발생한 공동편집이나 다음 workflow 변경까지
이전 execution의 snapshot으로 잘못 묶일 수 있다. 실제 callback을 활성화하기 전에 host가
verification에서 publication까지 같은 workbook-level lease를 유지하거나 base snapshot/revision
CAS로 재검증해야 한다. 응답에는 exact execution/manifest/base revision을 되돌려 주어야 하지만,
그 필드 검증만으로 저장 과정의 원자성이 생기지는 않는다.

API client는 기본적으로 `credentials: "same-origin"`을 사용한다. 따라서 운영 구성은 task pane과
API를 같은 인증 origin 또는 host-owned reverse proxy 아래 두는 것을 전제로 한다. bearer token
입력이나 범용 cross-origin 인증은 이 Add-in의 공개 계약이 아니다.

## 코드 경계

- `src/executor/api-client.ts`: 1MB 요청/3MB 응답 상한, action-scoped idempotency 재시도,
  workflow 응답 결합
- `src/executor/office-port.ts`: ExcelApi 1.13 feature gate, 셀/안전 제약 재조회,
  manifest 적용·재계산·save
- `src/executor/execute-approved-edit.ts`: claim/start/verify 상태 전이와 불확실성 분류
- `src/executor/persistence.ts`: 검증 후 새 snapshot host callback 계약
- `src/taskpane.ts`: exact diff 승인 UI와 실행 상태 제어
- `tests/`: fake Excel/HTTP 상태 전이 및 Python backend 공유 fixture 검증

Office.js에는 원자적 compare-and-set이 없다. 실행기는 write-start 왕복 뒤 한 번 더 before를
읽어 알려진 공동편집 변경을 막지만, 이것이 Excel transaction을 만드는 것은 아니다. 실제
Excel Desktop/Web에서 수식, 공동편집, save, host snapshot 연결을 확인하는 manual smoke는
배포 전 별도로 필요하다.

각 mutation key는 한 사용자 실행 시도에서 생성한 128-bit scope에 묶인다. 같은 HTTP 호출의
네트워크 재시도는 exact key/body를 재사용하지만, 다른 task pane이나 재클릭은 새 scope를 사용해
완료된 claim/start capability를 replay받지 않는다. claim/start 확인이 불명확하면 새 scope로
편집을 재실행하지 말고 backend workflow를 먼저 조정한다.

`npm run build`의 `dist/`는 정적 task pane asset만 만든다. localhost URL을 운영 URL로 바꾼
manifest, host bootstrap script, API reverse proxy/CORS, 인증 설정, 배포 작업은 별도 product
구성이다.
