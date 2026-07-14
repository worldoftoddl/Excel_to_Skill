# Repository Guidelines

## Project Structure & Architecture

`src/excel_to_skill/` contains the Python 3.11+ CLI. `cli.py` coordinates the deterministic
`convert`/`verify` path, legacy `annotate`/`review`, audit `prepare`, and bounded consumer
commands. Loaders and extractors build the workbook representation, while `emit_*.py` modules
write package artifacts.

The audit-RAG implementation lives in `src/excel_to_skill/audit/`:

- `extract.py`, `regions.py`, and `sources.py` create workbook-only facts with cell provenance.
- `auditpaper_mcp.py`, `standards.py`, and `context.py` retrieve and verify standards passages.
- `brief.py` synthesizes the agent-facing brief without blending workbook and standards sources.
- `agent.py` runs bounded read-only briefing/Q&A over committed artifacts and hydrates selected
  IDs back to workbook cells and verified standards locations.
- `aggregate.py` rolls independently committed sheet briefs into a compact account-oriented
  briefing without resending the workbook ledger or full standards context.
- `aggregate_agent.py` uses a committed aggregate as a conversation routing root, then resolves
  opaque aggregate/source refs back to the exact committed source sheet before final hydration.
- `conversation.py` compiles the persistent LangGraph `audit-chat` workflow; `conversation_store.py`
  keeps raw turn material outside checkpoints, and `langchain_client.py` provides the lazy
  `ChatAnthropic` structured-output boundary.
- `standards_research.py` compiles the uncheckpointed, turn-scoped MCP research worker; its child
  selects opaque candidates and application code re-fetches every selected CID.
- `procedure_planning.py` compiles the uncheckpointed proposed-test worker. It authors three to
  five bounded alternatives and combinations from exact typed workbook and standards basis refs
  without promoting them to prepared evidence.
- `workbook_source.py` binds an opaque uploaded asset to the package source digest;
  `workbook_inspection.py` provides bounded ledger-first range, dependency, profile, duplicate,
  outlier, and optional raw-XLSX observations without creating audit evidence.
- `xlsx_safety.py` validates a bounded XLSX/OOXML archive before it can become a server asset;
  `workbook_asset_service.py`, `workbook_asset_sqlite.py`, and `workbook_asset_web.py` keep
  principal-scoped raw workbook snapshots behind opaque upload, status, and download contracts.
- `service.py` maps opaque web bundle/thread commands to server-owned snapshots with
  principal-scoped runtime IDs and atomic idempotency claims; `web.py` exposes a lazy FastAPI
  POST/GET adapter without accepting package paths, providers, or model settings from clients.
- `workbook_edit.py` defines content-addressed proposal, exact-preview, approval, apply-manifest,
  executor-witness, and verification contracts for bounded Office edits without opening Excel.
- `workbook_edit_service.py` keeps that workflow outside conversations and prepared artifacts,
  consumes each exact approval once, and fences one live workbook execution at a time;
  `workbook_edit_web.py` exposes the strict host/add-in HTTP boundary.
- `workbook_edit_host.py` issues short-lived, principal-scoped host bootstrap selectors bound to
  one exact private session/workflow binding; `workbook_snapshot_publication.py` reacquires and
  validates saved XLSX bytes, then stores them behind immutable content-addressed asset refs.
- `workbook_edit_sqlite.py` is the durable single-database reference repository for workflow,
  idempotency, workbook-level fence/lease, and source-snapshot CAS state.
- `prepare.py` stages, validates, and atomically publishes all three artifacts.
- `consume.py` exposes commit-gated `brief`, search/get, assertion-procedure, and trace readers.
- `validate.py` enforces schemas, cross-links, digests, relation direction, and source separation.

JSON contracts live in `schemas/`; model instructions live in `prompts/`. Automated tests are in
`tests/test_*.py`; spreadsheet inputs are under `tests/fixtures/`, expected JSON/HTML/Markdown
output is under `tests/snapshots/`, and numbered notebooks provide supplemental manual checks.
Keep generated packages in ignored `converted/` or `tests/_output/` directories.

`office-addin/` is an independent TypeScript/Vite project for the ExcelApi 1.13 task pane. Its
executor consumes only the approved backend manifest, rereads live cells before and after the
write, and submits a bounded witness. Production bootstrap accepts only one opaque host-session
selector from the same authenticated HTTPS origin. Required persistence saves the workbook, asks
the server to reacquire and publish a new source snapshot, and recovers ambiguous POST results by
execution-scoped GET without replaying the cell edit.

## Audit-RAG Contracts

The prepared audit package intentionally separates three trust domains:

- `data/audit_facts.json`: only facts documented in the workbook, bound to real ledger cells.
- `data/standards_context.json`: MCP-retrieved audit/accounting standards with pinned collection
  and verified CID text.
- `data/audit_brief.json`: synthesis that cites workbook fact IDs and standards citation IDs
  separately.

Preserve these invariants when changing the audit path:

- Cell addresses are a provenance layer, not the product goal. Every fact must retain at least
  one current-region source and a content digest. A carried header/legend may be an additional
  `label` source but can never be the sole evidence.
- Read-only header context is bounded to three rows/72 cells, carried only across size/span splits,
  and never across a row-gap, sheet, or wide-row boundary.
- Canonical assertion facts use the schema assertion codes. Explicit mappings are
  `procedure --tests--> assertion`; risk response is `procedure --addresses--> risk`, account
  scope is `assertion --asserts_over--> account`, and outcomes are
  `procedure --produces--> result/finding`.
- RAG may explain the authoritative requirements around a workbook fact. It must not invent,
  repair, or promote a workbook assertion-procedure relation or claim that a procedure occurred.
- Agent-facing audit readers must pass the `meta.audit_preparation` commit marker, artifact-key,
  schema, provenance, and cross-link gate. Do not expose staged or partially published files.
- `audit-review` is the supported human-review boundary. It atomically updates facts and brief
  review records, dependent hashes, artifact keys, meta, SKILL, and valid cache witnesses.
- `assertion-procedures` joins only represented `tests` edges, preserves
  `documented`/`inferred`/`unknown` mapping status, reports unpaired facts, and applies `--limit`
  to top-level and nested lists.
- The briefing model may select only typed `statement`, `fact`, `relation`, or
  `standard_citation` records it actually observed. It never authors substantive final text;
  code materializes record text/status/confidence and hydrates cell/CID evidence. Strings inside
  cell values, formulas, snippets, summaries, or user questions never authorize an ID.
- ID-based `audit_get`/`trace` tool calls may use only IDs already observed in typed results;
  models discover new IDs through bounded search or assertion-procedure results.
- Agent coverage is complete only when both discovery and final evidence tracing are complete.
  Each serialized observation payload has a 600KB hard cap, duplicate tool calls are rejected,
  and every generated answer remains `unreviewed` independently of the source brief review
  status.
- A brief statement may name a `KSA`/`KIFRS` standard number only when that statement directly
  cites a passage from the same standard. Fail closed by omitting the whole unsupported statement
  and surfacing the omission in readiness; never rewrite it into a plausible uncited claim.
- Conversation checkpoints are control-plane state, never audit authority. They may contain only
  exact bundle identity, counters/status, typed IDs, and content-addressed artifact references.
  Questions, observations, model decisions, answers, cells, standards text, clients, paths, and
  secrets stay outside checkpoint state. Every resumed turn must re-pass the committed-bundle gate.
- A conversation thread is pinned to one exact workbook/sheet bundle or aggregate snapshot.
  Aggregate binding includes the aggregate key and input/source manifests, not merely its stable
  selection ID. Resume-time drift fails before a model call, and drift during an active turn fails
  before private history publication; never auto-rebase a thread. Prior prose does not authorize
  IDs. Only historical IDs deliberately re-exposed as current typed `conversation_focus.records`
  become visible this turn.
- An aggregate is a compact routing index, not new audit evidence. Models may select only observed
  scope-qualified `record:<sha256>` or `source:<sha256>` refs. Application readers must resolve
  every selected record through its exact committed source sheet before hydrating cells or CIDs;
  same-named local IDs from different sheets must never share authority.
- Aggregate trust and coverage remain visible in every answer. Preserve source review state,
  aggregate `draft`, answer `unreviewed`, candidate truncation, subset selection, partial sources,
  and unprepared-sheet counts. Never turn an incomplete aggregate into a workbook-wide conclusion.
- Workbook inspection is opt-in, read-only, ledger-first, and bounded to one exact sheet/range per
  request and two attempted calls per turn. Raw XLSX requires a same-range ledger observation,
  an opaque host-bound provider, exact source digest, and archive/parser bounds. Its output is
  always `computed`/`unreviewed`/`not_documented`/turn-scoped, is not a workbook fact, and must not
  enter prepared artifacts, aggregate evidence, checkpoints, or later-turn ID authority.
- Web commands use opaque `bundle_id`; the host alone resolves package/runtime paths and optional
  raw workbook providers. Public thread IDs must be deterministically namespaced by tenant and
  subject before graph persistence, while receipts expose only the public ID. Claim an
  Idempotency-Key before entering the runtime; a started but unpublished turn fails closed as
  pending rather than being executed twice.
- `.audit_runtime/conversations/` is a private, ignored runtime store, not a prepared artifact or
  review boundary. Its canonical objects and SQLite checkpoints must remain refs-only separated,
  digest-checked, thread-scoped, and created with private permissions where supported.
- Persist graph-node failures as fixed codes only; provider exception text must never enter the
  checkpoint error channel. Hash user-facing thread IDs before using them as SQLite checkpoint
  keys, and validate usage metadata before publishing turn history.
- Procedure planning is an opt-in authoring boundary, not extractive evidence. Require exactly one
  observed risk and assertion, preserve separate workbook/standards basis refs, and return three
  to five distinct candidates with exactly one `primary` plus `alternative` and `complementary`
  options. Each candidate must retain applicability conditions, evidence, strengths, limitations,
  prerequisites, open questions, and fixed quantitative `TBD` fields; combinations must reference
  at least two proposed candidates.
- Every proposed candidate remains `proposed`/`unreviewed`/`not_evidenced` and outside prepared
  artifacts. Never merge a plan into facts, briefs, aggregates, or documented `tests`/`addresses`/
  `produces` edges. Aggregate planning may use only observed `source:<sha256>` refs from one exact
  source scope. A research basis must be from the current turn and match that scope and collection.
  Raw planning input/output stays in private objects behind checkpoint refs and is never re-exposed
  as later-turn focus or ID authority.
- Workbook editing is a separate authoring workflow, never an `audit-chat` tool side effect. V1 is
  limited to at most 100 single-cell edits on one exact sheet: literal value, allowlisted formula,
  number format, or clear contents. Merged, spill, protected, and table-member targets fail closed;
  formulas are same-sheet, locale-neutral, statically bounded, and may not introduce an unapproved
  spill range.
- A human approval binds one exact live-cell preview digest and expires before write start. The
  executor must claim a one-use approval, retain the monotonic fence and challenge, reread the
  exact before state, mark write start, apply only the immutable manifest, recalculate when required,
  and return an exact after-state witness within the server-issued execution deadline. A stale
  precondition is a no-write terminal result; `verification_failed` and `indeterminate` quarantine
  the live workbook until host reconciliation.
- Add-in mutation idempotency is scoped to one 128-bit client action and remains stable only for
  that action's internal network retries. Never use a workflow-global deterministic key that can
  replay a completed claim/start capability into another task pane or executor.
- The host must register a stable `workbook_instance_id` shared by coauthoring sessions and
  distinct across workbook copies. Revision, sheet, and worksheet remain manifest preconditions,
  not ways to reset workbook-level fencing. Completed command replay and stored workflow reads do
  not depend on a still-live Office session.
- A host-session ID is an opaque selector under an already authenticated principal, never a bearer
  credential. Production Add-ins accept only that selector, fetch bootstrap from the current HTTPS
  origin with same-origin credentials, and bind every mutation to its exact workflow/session and
  private binding digest. Authenticate before reading mutation bodies; expiry, revocation, and
  principal scope remain mandatory even for immutable completed-workflow/publication reads.
- `session_verified` means a host-authenticated executor's bounded readback is internally
  consistent with the approved authored state. It does not mean the backend independently ran
  Excel or validated every formula result or dependent cell. Under `required` persistence it is a
  nonterminal state: retain the workbook-level publication lease until the server has reacquired
  the saved XLSX, matched every approved authored cell/formula and number format, stored exact bytes,
  and atomically advanced the source head from the exact base bundle/snapshot/hash/revision.
- Pin `required`, `session_only`, or `unsupported` to the execution at claim time; do not infer
  lease release from a server-global provider setting. Different publication idempotency keys for
  one execution share a single bounded publication claim. Durable claims may be reclaimed only
  after expiry with a new token that fences every older worker.
- Snapshot publication requests carry only exact execution and manifest selectors. Provider
  locators, workbook bytes, physical workbook identity, new digest, revision, and asset refs are
  server-owned. The reacquirer must prove the saved revision is the direct provider transition from
  the pinned base and attest the exact worksheet identity; reading whichever revision is newest is
  forbidden. Validate the XLSX archive and every approved authored cell/formula and number format
  before immutable storage. An asset written before a losing source-head CAS is only an orphan
  candidate and must never become a source head. POST response loss is recovered through
  execution-scoped GET and may never rerun the manifest or save the workbook again. A published raw
  snapshot is not a prepared audit bundle.
- Local immutable storage and SQLite are reference implementations, not the cloud product boundary.
  Production still needs authenticated durable host/session registries, a provider reacquirer,
  object storage, multi-worker deployment tests, pending/orphan reconciliation, and failed or
  indeterminate workbook quarantine handling.
- A web upload creates only a principal-scoped `workbook_id` and immutable `raw_snapshot_id`; it
  does not create a prepared `bundle_id`. Authenticate and validate headers before consuming the
  size- and time-bounded XLSX body, reject non-XLSX or polyglot OOXML archives, validate before
  immutable storage, and publish the workbook, snapshot, head, and completed idempotency receipt
  in one repository transaction. Public status and download responses may expose the exact digest
  and size but never the private asset ref or a server path. Only a later convert/prepare commit
  gate may publish a `bundle_id` for audit readers.

## Build, Test, and Development Commands

```bash
uv sync                         # install the core and development dependencies
uv sync --extra annotate        # also install Anthropic/LangSmith integrations
uv sync --extra prepare         # also install Anthropic/FastMCP audit preparation dependencies
uv sync --extra graph           # install LangGraph, SQLite checkpoint, and ChatAnthropic support
uv sync --extra inspection      # install pandas for bounded table analytics
uv sync --extra web --extra graph  # install web service and audit-chat runtime support
uv run pytest                   # run the complete automated test suite
uv run pytest tests/test_review.py  # run one focused test module
uv run excel-to-skill convert tests/fixtures/fx1_merge_formula.xlsx
uv run excel-to-skill prepare <converted-package> --force
uv run excel-to-skill audit-review <prepared-package> --approve
uv run excel-to-skill assertion-procedures <prepared-package> --query 완전성
uv run excel-to-skill trace <prepared-package> --id <fact-or-relation-id>
uv run excel-to-skill audit-agent <prepared-package> --question "핵심 미비점은?"
uv run --extra graph excel-to-skill audit-chat <prepared-package> --question "핵심 위험은?"
uv run --extra graph excel-to-skill audit-chat <prepared-package> --thread <id> --question "그 결과는?"
uv run --extra graph excel-to-skill audit-chat <prepared-package> --aggregate-id <id> --question "계정별 위험은?"
uv run --extra graph --extra prepare excel-to-skill audit-chat <prepared-package> --question "외부조회 관련 감사기준은?" --standards-research
uv run --extra graph excel-to-skill audit-chat <prepared-package> --question "이 위험에 가능한 test는?" --procedure-planning
uv run --extra graph --extra prepare excel-to-skill audit-chat <prepared-package> --question "기준을 확인하고 test들을 비교해줘" --standards-research --procedure-planning
uv run --extra graph --extra inspection excel-to-skill audit-chat <prepared-package> --question "C시트 J열을 분석해줘" --workbook-inspection
uv build                        # build wheel and source distributions with Hatchling
cd office-addin && npm ci       # install the isolated Office.js development dependencies
npm test && npm run build       # run Vitest and compile/build the task pane
npm run validate:manifest      # validate the localhost development manifest
```

Run `verify <package> --source <workbook>` when changing deterministic conversion behavior or
prepared-artifact publication. `prepare`, `brief`, `audit-agent`, `audit-chat`, and all audit consumer commands
operate on a converted package directory, not directly on the source workbook.

## Coding Style & Naming Conventions

Follow the established Python style: four-space indentation, type hints, and `from __future__ import annotations`. Use `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_CASE` for constants, and a leading underscore for internal helpers. Keep identifiers in English; Korean comments and user-facing text are acceptable where they match the surrounding module. No formatter or linter is configured, so preserve local formatting and keep changes narrowly scoped.

## Testing Guidelines

Name modules `test_<area>.py` and tests `test_<behavior>`. Prefer `tmp_path`, parametrization, and stub clients; unit tests must not require live API calls. Refresh snapshots intentionally with `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_v9_fixtures.py`, then review every diff. There is no configured coverage threshold, but behavioral fixes should include regression tests.

Audit changes should normally run the relevant `tests/test_audit*.py` modules plus
`tests/test_assertion_procedures.py`, followed by the complete suite. Provider/MCP behavior must
use injected stubs in automated tests. If a live smoke test is needed, use a non-sensitive
synthetic workbook and record separately that its brief remains draft until reviewed.
Graph tests should use an in-memory saver for routing and isolation, plus focused SQLite restart
tests. Inspect checkpoint payloads to ensure raw questions, answers, cells, standards text, and
secrets never entered the database. The core import/convert/verify path must still work without
the optional `graph` extra. Inspection tests must cover exact Excel grid/range limits, source
digest mismatch, raw archive expansion bounds, lazy pandas import, truncated auxiliary-reference
coverage, aggregate exact-source routing, and the absence of inspection payloads from checkpoints
and later-turn focus. Web tests should exercise the framework-neutral adapter and actual ASGI
POST/GET requests; do not depend on Starlette `TestClient` when the installed portal/http client
combination is incompatible. Production repositories need multi-worker claim/publish/abort and
principal-isolation tests; the in-memory implementations prove only the single-process contract.
Workbook-edit tests must additionally cover exact preview digest binding, no-op/unsafe target and
formula rejection, approval expiry and single use, stale-before-write, post-start retry denial,
monotonic workbook-level fencing, cross-session/cross-sheet isolation, indeterminate quarantine,
idempotent replay after session loss, repository fault atomicity, and executor reread verification.
Add-in tests use fake Excel/HTTP ports and a Python-generated shared contract fixture; they must
cover misrouted workflow responses, claim/start/verify uncertainty, request/response and witness
bounds, coauthor drift rereads, formula recalculation, read-only workbooks, save/publication loss,
same-origin host bootstrap/header binding, publication-only recovery, and host callback validation.
Publication tests must reopen the reacquired XLSX and compare every approved authored cell/formula
and number format, exercise immutable-store integrity and source-head CAS, and restart a durable
repository while an active publication lease exists. A real Excel Desktop/Web sideload smoke and
real cloud-provider save/reacquisition remain manual host checks.
Raw-workbook upload tests must cover authentication before body consumption, exact byte limits,
finite body-read admission, strict ZIP/OOXML framing, malformed and polyglot rejection,
idempotent replay and conflict, expired-claim fencing, SQLite restart/concurrency, principal
isolation, immutable-store readback, atomic head publication, and download digest verification.

## Current Audit-RAG Status

The audit-RAG path is now the local `main` direction. The last committed checkpoint is `cce3954`;
the current working slice adds the provider-neutral raw-workbook upload and snapshot catalog in
front of the existing prepare/chat path. The former local
main harness series remains only at `archive/harness-v1.20`. The current checkpoint includes region-wide
fact extraction, remote auditpaper standards MCP retrieval, collection-pinned CID verification,
persistent paragraph caching, agent-ready brief generation, commit-gated readers, canonical
management assertions, deterministic assertion-procedure queries, and a bounded extractive
briefing/Q&A agent. The current brief contract is `audit_brief.v2`; rerunning `prepare` upgrades
a v1 brief while reusing valid upstream stages.

The historical pre-aggregate baseline recorded here was `344 passed, 1 skipped`; prior wheel and
source-distribution build checks passed. Sheet-scope account aggregation and the persistent
conversation graph have since been implemented. The graph now accepts either one committed
workbook/sheet bundle or `--aggregate-id`: aggregate records remain a compact root, opaque refs
route to exact committed source sheets, and final cells/CIDs are hydrated there. Focused aggregate
conversation, persistence, CLI, and trust/coverage regression tests pass. Optional MCP-backed
standards research now adds isolated candidate selection, selected-CID re-verification, and
turn-scoped supplemental responses without checkpoint or prepared-artifact authority. Optional
procedure planning now uses exact observed risk/assertion and standards basis to produce three to
five `primary`/`alternative`/`complementary` test options, per-option conditions and evidence
trade-offs, and recommended combinations. Plans remain proposed, unreviewed, not evidenced, and
outside every prepared artifact; when committed standards context is insufficient, the main graph
may use a same-turn, same-scope research result first. Optional workbook inspection now adds a
ledger-first deterministic branch for exact range reads, formula dependencies, pandas profiles,
duplicates, outliers, and a digest-bound raw XLSX re-read when a host provider is present. Selected
inspection refs remain computed, unreviewed, not documented, turn-scoped supplements and never
become prepared or later-turn evidence. A web-ready service boundary now resolves opaque bundle
IDs, namespaces public threads per principal, claims idempotency keys before runtime entry, and
offers strict FastAPI POST/GET adapters over server-owned snapshots. The in-memory repository and
lock are prototype implementations; durable multi-worker storage remains a host responsibility.
The approved-edit backend now adds content-addressed proposals and exact live-cell previews,
digest-bound human approval, one-use execution claims, workbook-level monotonic fencing, bounded
formula and safe-number policy, execution leases, reread verification, failure quarantine, strict
request/response schemas, and bounded FastAPI endpoints. The Add-in slice now provides
an ExcelApi 1.13 task pane, exact cell and safety-constraint rereads, immutable manifest execution,
worksheet recalculation, bounded witness submission, policy-controlled current-workbook save, and
publication-only resume. Production bootstrap now accepts one short-lived principal-scoped selector
from the same HTTPS origin. Required persistence retains the workbook lease, enforces one bounded
publication claimant, requires a provider-attested direct revision and worksheet identity, validates
the saved XLSX before immutable storage, and atomically advances the source head in the in-memory or
SQLite repository. SQLite preserves workflow/idempotency/fence/claim/head state across restarts and
reclaims expired command/publication workers with new fencing tokens. Real host authentication,
durable session registries, cloud-provider reacquisition, production object storage, quarantine
reconciliation, and raw-snapshot-to-prepared-bundle publication remain product integrations. The
current complete Python suite is `863 passed, 1 skipped`; the focused raw-upload plus adjacent
inspection/publication suite is `85 passed`, the focused workbook-edit/publication suite is
`190 passed`, the Add-in suite is `87 passed`, and compileall, TypeScript/Vite, wheel, and
source-distribution builds pass.

The adopted product default is provider-neutral web upload, not Microsoft 365. A normal user
uploads XLSX to server-owned object storage, runs prepare/aggregate, chats over the committed
bundle, reviews edit proposals, and receives a revised workbook copy. OneDrive/SharePoint plus the
Office.js executor is an optional direct-edit integration for Microsoft 365 and coauthoring users;
it is not a prerequisite for the web UI or the core audit briefing product.

The latest raw-upload smoke used the non-client 36-sheet 2025 K-IFRS account-procedure template.
Its 352,145 bytes passed strict XLSX validation, immutable upload, SQLite restart, idempotent
replay, status/download, and digest-bound source-provider checks with exact byte and SHA-256
equality. This smoke stores a raw source only; it does not create or approve a prepared bundle.

The current orchestration plan is intentionally staged:

1. Maintain the verified `audit-chat` slices: compiled dynamic tool/final routing, restart-safe
   threads, exact workbook/sheet/aggregate pinning, typed prior-turn focus, refs-only checkpoints,
   exact-source aggregate tracing, and request-level usage. Before long-running production use,
   add a bounded retention/GC policy for orphaned private objects left by failed or abandoned
   invocations.
2. Maintain the optional dynamic standards-research slice for questions outside committed
   context: lazy opt-in MCP access, pinned collection and exact aggregate source scope, isolated
   candidate selection, selected-CID re-fetch, turn-scoped `ephemeral`/`unreviewed` output, and no
   checkpoint or prepared-artifact authority.
3. Maintain the optional procedure-planning slice for one observed risk/assertion target: three
   to five role-diverse candidates, per-option applicability/evidence/trade-offs, recommended
   combinations, exact-scope refs, and fixed `proposed`/`unreviewed`/`not_evidenced` status. When
   needed, use only current-turn verified research as a fallback standards basis; never promote a
   proposal into documented workpaper evidence.
4. Maintain the optional workbook-inspection slice: package-ledger first, one exact source
   sheet/range, two request attempts per turn, bounded deterministic analytics, opaque
   digest-bound raw source only when the host supplies it, refs-only checkpoints, and no
   later-turn authority.
5. Stabilize the web service boundary around opaque bundle snapshots and repository interfaces.
   The current product slice adds bounded XLSX upload, a principal-scoped raw snapshot catalog,
   exact status/download, and a digest-bound source provider without prematurely creating a
   prepared bundle. The next slice connects that raw snapshot to deterministic convert, scope
   planning, prepare/aggregate jobs, and commit-gated `BundleSnapshot` publication. Later slices
   add server-copy edit proposal/approval and a downloadable revised workbook. Replace the
   in-memory receipt/idempotency/turn-lock
   implementations with durable DB/object storage and a distributed claim/lock plus pending-claim
   reconciliation before multi-worker production use. Keep the current API synchronous until a
   job/queue product contract is chosen.
6. Maintain the separate approved-edit backend and Office.js executor: propose bounded edits,
   preview an exact live-cell diff, bind human approval, atomically claim a workbook-level fence,
   apply only the immutable manifest, and verify the executor's reread without promoting it to
   prepared evidence. Maintain same-origin authenticated bootstrap, execution-scoped publication
   claims, direct provider-revision/worksheet attestation, pre-store XLSX validation, immutable
   assets, source-head CAS, and publication-only recovery. Treat this as an optional Microsoft 365
   integration. A future direct-edit slice may add a OneDrive/SharePoint-style reacquirer, durable
   host/session registries, reconciliation UI/jobs, and raw-snapshot-to-new-bundle preparation,
   but it must not block the provider-neutral web product. Never grant arbitrary JavaScript or
   Python execution through the conversation model.
7. Move `prepare` orchestration to a graph only after replacing temporary staging with durable,
   crash-resumable staging that preserves commit-last publication and rollback guarantees.
8. Keep the current single-call aggregate generation path outside the graph until it needs
   genuine dynamic branching; do not migrate it merely for framework uniformity.

The latest live synthetic receivables workpaper regenerated
`audit_facts` with extractor 0.2.1 and published `audit_brief.v2`/0.4.3. It produced two
documented mappings—existence to an external-confirmation/reconciliation procedure and
completeness to a shipping-document-to-ledger trace—with no unpaired assertions or procedures.
Four standards queries and 25 verified citations succeeded against
`standards_20250829_bgem3`. Readiness remained `partial` because framework/effective-date
identity was not fully structured, while no workbook open-item facts remained. The live briefing
agent selected `tests` and `produces` relations, results, conclusions, gaps, exact cells, CID
locations, and bounded original standards excerpts; deterministic hydration was complete and a
second `prepare` was a no-network cache hit. This smoke result proves the pipeline wiring, not
human approval of a real audit workpaper; generated briefs and agent answers remain
`draft`/`unreviewed` unless explicitly reviewed.

The latest procedure-planning synthetic live smoke pinned dynamic research to
`standards_20250829_bgem3_v3`, re-fetched `KSA::330::A51`, `KSA::330::A50`, and
`KSA::330::A55` with `standards_get_paragraph`, and passed those same-turn ephemeral refs to the
real Anthropic planning worker. It returned five candidates—one primary, two alternatives, and two
complementary tests—plus two two-test combinations. Every option retained non-empty applicability
conditions, evidence, strengths, and limitations, while the plan remained
`proposed`/`unreviewed`/`not_evidenced` and the research remained
`ephemeral`/`unreviewed`/turn-scoped/outside the prepared bundle. The two child-model requests used
18,075 total tokens in that deterministic routing smoke; main-agent routing remains separately
covered by compiled-graph integration tests and live research selection.

The latest deterministic inspection smoke used the non-client 2025 K-IFRS account-procedure
template with 36 non-empty sheets, 5,895 emitted cells, and 59 workbook regions. Without an LLM
call, a C-sheet range inspection returned 24 relevant ledger cells, dependency inspection found
the four exact C-to-A formula precedents, table profiling described the 11-column procedure area,
and duplicate analysis found two repeated assertion-code groups. A host-bound raw provider with
the same source digest re-read `C4:G5` and preserved formulas plus cached values. This demonstrates
bounded sheet/range reinspection, not audit approval or a workbook-wide conclusion.

## Commit & Pull Request Guidelines

Recent commits use concise milestone or scope prefixes followed by a concrete outcome, for example `M3 4b단계 - 승계 규칙(...)` or `verify: ... 결함 보정`; Conventional Commits are not required. Keep each commit focused. Pull requests should explain behavior and risk, list verification commands, link relevant issues/spec sections, and include representative snapshot diffs or rendered output when layout generation changes.

## Security & Configuration

Keep API keys in the ignored `.env`. `ANTHROPIC_API_KEY` is needed for annotation, audit
preparation, `audit-agent`, aggregation, and `audit-chat`; `MCP_AUTH_TOKEN` authenticates the remote standards MCP; LangSmith keys enable
optional tracing. The client does not need direct Qdrant credentials when it uses the remote MCP.
Use environment placeholders such as `${MCP_AUTH_TOKEN}` in `.mcp.json`; never commit a literal
token or copy a credential from chat into source, tests, logs, or documentation.

Converted workbooks and prepared audit artifacts may expose source cell data, and `--full-names`
emits defined-name values. Treat generated packages and standards caches as sensitive and do not
commit them without review. Do not send a real client workbook to an external model or MCP merely
to test wiring; use a synthetic fixture unless external processing of that workbook is explicitly
within scope. `audit-agent` sends bounded prepared-package observations to the configured model;
it does not call the standards MCP again. A model-requested `trace` can send selected raw cell
values/formulas, optional LangSmith tracing can copy the same exchange externally, and `--json`
prints hydrated raw cells. `audit-chat` persists questions and hydrated answers under the private
`.audit_runtime/` directory and its LangChain calls can be traced by the same environment keys; it
uses hashed SQLite checkpoint thread keys while returning the friendly thread ID to the caller,
and does not call the standards MCP by default. With `--standards-research`, URL/token resolution
and MCP access remain lazy until the conversation model selects that tool; selected CIDs are
re-fetched by application code and returned only as turn-scoped `ephemeral`/`unreviewed` context.
With `--procedure-planning`, an isolated child may author multiple proposed tests from exact typed
basis refs, but those outputs remain private turn-scoped supplements and never become performed
facts. Enabling both flags permits research-first fallback only when the main agent selects it;
planning itself does not connect to MCP. Aggregate-bound research and planning require one exact
exposed source scope, and neither child inherits the parent checkpointer. With
`--workbook-inspection`, package-ledger observations or host-bound raw ranges may be included in the
model exchange and public answer, but they remain computed supplements; raw providers must never
expose an asset locator and must match the committed source digest. Web clients may provide only
opaque bundle/thread IDs. Keep server paths, provider objects, internal principal-scoped runtime
thread IDs, and idempotency claim tokens outside receipts and logs. The bundled in-memory
repository/lock is not a multi-worker production store. Use synthetic data for live tests and
explicitly blank both LangSmith key variables when tracing must stay off.
