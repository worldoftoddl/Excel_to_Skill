# Audit Workbook Office Add-in

이 디렉터리는 Python workbook-edit backend가 발행한 승인 manifest를 실제 Excel 세션에
적용하는 Office.js task pane이다. 모델이 JavaScript를 실행하는 도구가 아니며, 다음 고정
순서만 수행한다.

이 Add-in은 웹 UI의 필수 구성요소가 아니다. 일반 웹 사용자는 XLSX를 서버 object storage에
업로드해 분석·대화하고 수정본을 내려받을 수 있다. 이 디렉터리는 OneDrive/SharePoint 기반
Excel Web·공동편집 파일에 승인된 변경을 직접 반영하려는 Microsoft 365 사용자를 위한 선택 기능이다.

```text
workflow 조회 → 현재 셀 재조회 → exact preview → 사용자 승인
→ one-use claim/fence → write-start 확인 → before 재조회
→ immutable manifest 적용 → 필요 시 worksheet 재계산
→ after 재조회 → backend 검증 → 정책에 따른 Workbook.save()
→ server 재취득·검증 → immutable asset 저장 → source-head CAS
```

`session_verified`는 현재 Excel 세션의 authored state 재조회가 승인 상태와 일치했다는
뜻이다. `persistence_policy=required`일 때는 아직 중간 상태이며, backend가 저장된 XLSX를
다시 취득해 승인된 셀을 확인하고 새 raw-workbook snapshot을 CAS로 게시해야 완료된다.
게시된 raw snapshot도 자동으로 prepared audit bundle이 되는 것은 아니다.

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
proposal 생성이나 Office session 등록 UI는 포함하지 않는다. production build에서는 자유 URL과
workflow 입력을 사용하지 않는다. 배포 host가 인증된 principal, 현재 Office session, workflow를
서버에 결합한 뒤 짧게 유효한 opaque host-session ID 하나만 task pane에 주입한다.

```ts
window.auditWorkbookEditHost = {
  hostSessionId: "edit-host-<128-bit-random-hex>",
};
```

task pane은 현재 HTTPS origin에서만 bootstrap을 읽고, 이후 모든 workflow/publication 요청에 같은
`X-Audit-Workbook-Host-Session`을 보낸다. 이 값은 인증 cookie를 대신하는 bearer token이 아니라
이미 인증된 principal 아래 exact binding을 선택하는 추가 식별자다. 서버는 principal, 만료·폐기,
workflow/session, private binding digest를 모두 다시 확인한다.

`persistence_policy`별 동작은 다음과 같다.

- `required`: save를 강제하고 `session_verified` 뒤 서버 publication까지 workbook-level lease를 유지한다.
- `session_only`: 현재 세션 재조회 검증까지만 수행하며 사용자가 save 여부를 선택할 수 있다.
- `unsupported`: Add-in의 save/publication을 비활성화한다.

`required` publication 요청에는 execution과 manifest ref/hash만 들어간다. 서버 소유 reacquirer가
provider ETag/version history로 pinned base의 direct successor임을 확인한 XLSX bytes를 읽고, exact
worksheet identity와 최대 100개 authored cell/formula·number format을 재검증한다. 통과한 bytes만
SHA-256 content-addressed immutable asset에 저장되고, base bundle/snapshot/hash/revision과 workbook
fence가 한 번의 repository CAS에서 새 source head로 바뀐다. CAS가 완료될 때만 publication lease가
해제된다.

POST 응답이 유실되거나 task pane이 다시 열리면 같은 manifest를 재실행하지 않는다. execution ID의
GET publication을 먼저 조회하고, 아직 `session_verified`라면 save/publication 단계만 재개한다.
재개 시 workbook을 다시 save하지도 않는다. 서버 reacquirer가 최초 save의 exact direct successor를
찾지 못하면 host reconciliation으로 닫힌다. 고정된 idempotency key와 publication-only lookup이 완료
응답 유실을 흡수한다.

운영 환경에서 아직 host가 제공해야 하는 부분은 다음이다.

- 인증된 principal과 현재 workbook을 backend `WorkbookSessionBinding`에 등록
- 공동편집 세션에서는 같은, 복사본에서는 다른 stable `workbook_instance_id` 발급
- 같은-origin API authentication과 host-session 발급·폐기의 durable registry 구현
- OneDrive/SharePoint 등 실제 저장 provider의 server-owned reacquirer 구현
- local content-addressed store를 S3/MinIO 등 운영 object storage로 교체
- 여러 서버 노드용 운영 DB와 pending claim, orphan asset, 실패 격리 조정
- 게시된 raw snapshot을 새 converted/prepared audit bundle로 연결하는 후속 pipeline

`HostCallbackSnapshotPublisher`는 기존 embedding host와 단위 테스트용 integration contract로
남아 있다. production task pane은 `AuthenticatedApiSnapshotPublisher`를 사용한다. 불확실 오류가
표시되면 같은 셀 편집을 다시 적용하지 말고 publication GET과 host 상태를 먼저 조정해야 한다.

## 코드 경계

- `src/executor/api-client.ts`: 1MB 요청/3MB 응답 상한, action-scoped idempotency 재시도,
  workflow 응답 결합
- `src/executor/office-port.ts`: ExcelApi 1.13 feature gate, 셀/안전 제약 재조회,
  manifest 적용·재계산·save
- `src/executor/execute-approved-edit.ts`: claim/start/verify 상태 전이와 불확실성 분류
- `src/executor/persistence.ts`: same-origin snapshot publication, idempotent POST, GET recovery
- `src/host-bootstrap.ts`: exact host-session 설정, bounded bootstrap 조회와 workflow 결합 검증
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
manifest, host-session 발급 script, API reverse proxy, 인증 설정, cloud reacquirer/object store,
배포 작업은 별도 product 구성이다.
