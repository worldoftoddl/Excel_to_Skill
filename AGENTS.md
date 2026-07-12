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
- `prepare.py` stages, validates, and atomically publishes all three artifacts.
- `consume.py` exposes commit-gated `brief`, search/get, assertion-procedure, and trace readers.
- `validate.py` enforces schemas, cross-links, digests, relation direction, and source separation.

JSON contracts live in `schemas/`; model instructions live in `prompts/`. Automated tests are in
`tests/test_*.py`; spreadsheet inputs are under `tests/fixtures/`, expected JSON/HTML/Markdown
output is under `tests/snapshots/`, and numbered notebooks provide supplemental manual checks.
Keep generated packages in ignored `converted/` or `tests/_output/` directories.

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
- `assertion-procedures` joins only represented `tests` edges, preserves
  `documented`/`inferred`/`unknown` mapping status, reports unpaired facts, and applies `--limit`
  to top-level and nested lists.

## Build, Test, and Development Commands

```bash
uv sync                         # install the core and development dependencies
uv sync --extra annotate        # also install Anthropic/LangSmith integrations
uv sync --extra prepare         # also install Anthropic/FastMCP audit preparation dependencies
uv run pytest                   # run the complete automated test suite
uv run pytest tests/test_review.py  # run one focused test module
uv run excel-to-skill convert tests/fixtures/fx1_merge_formula.xlsx
uv run excel-to-skill prepare <converted-package> --force
uv run excel-to-skill assertion-procedures <prepared-package> --query 완전성
uv run excel-to-skill trace <prepared-package> --id <fact-or-relation-id>
uv build                        # build wheel and source distributions with Hatchling
```

Run `verify <package> --source <workbook>` when changing deterministic conversion behavior or
prepared-artifact publication. `prepare`, `brief`, and all audit consumer commands operate on a
converted package directory, not directly on the source workbook.

## Coding Style & Naming Conventions

Follow the established Python style: four-space indentation, type hints, and `from __future__ import annotations`. Use `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_CASE` for constants, and a leading underscore for internal helpers. Keep identifiers in English; Korean comments and user-facing text are acceptable where they match the surrounding module. No formatter or linter is configured, so preserve local formatting and keep changes narrowly scoped.

## Testing Guidelines

Name modules `test_<area>.py` and tests `test_<behavior>`. Prefer `tmp_path`, parametrization, and stub clients; unit tests must not require live API calls. Refresh snapshots intentionally with `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_v9_fixtures.py`, then review every diff. There is no configured coverage threshold, but behavioral fixes should include regression tests.

Audit changes should normally run the relevant `tests/test_audit*.py` modules plus
`tests/test_assertion_procedures.py`, followed by the complete suite. Provider/MCP behavior must
use injected stubs in automated tests. If a live smoke test is needed, use a non-sensitive
synthetic workbook and record separately that its brief remains draft until reviewed.

## Current Audit-RAG Status

`audit-rag-v0` is implemented as of commit `07f75e9`. The current checkpoint includes region-wide
fact extraction, remote auditpaper standards MCP retrieval, collection-pinned CID verification,
persistent paragraph caching, agent-ready brief generation, commit-gated readers, canonical
management assertions, and deterministic assertion-procedure queries.

The current verified baseline is `287 passed, 1 skipped`; wheel and source-distribution builds
pass. A live synthetic receivables workpaper produced two documented mappings—existence to an
external-confirmation/reconciliation procedure and completeness to a shipping-document-to-ledger
trace—with no unpaired assertions or procedures. Four standards queries succeeded against
`standards_20250829_bgem3`. This smoke result proves the pipeline wiring, not human approval of a
real audit workpaper; generated briefs remain `draft`/`unreviewed` unless explicitly reviewed.

## Commit & Pull Request Guidelines

Recent commits use concise milestone or scope prefixes followed by a concrete outcome, for example `M3 4b단계 - 승계 규칙(...)` or `verify: ... 결함 보정`; Conventional Commits are not required. Keep each commit focused. Pull requests should explain behavior and risk, list verification commands, link relevant issues/spec sections, and include representative snapshot diffs or rendered output when layout generation changes.

## Security & Configuration

Keep API keys in the ignored `.env`. `ANTHROPIC_API_KEY` is needed for annotation and audit
preparation; `MCP_AUTH_TOKEN` authenticates the remote standards MCP; LangSmith keys enable
optional tracing. The client does not need direct Qdrant credentials when it uses the remote MCP.
Use environment placeholders such as `${MCP_AUTH_TOKEN}` in `.mcp.json`; never commit a literal
token or copy a credential from chat into source, tests, logs, or documentation.

Converted workbooks and prepared audit artifacts may expose source cell data, and `--full-names`
emits defined-name values. Treat generated packages and standards caches as sensitive and do not
commit them without review. Do not send a real client workbook to an external model or MCP merely
to test wiring; use a synthetic fixture unless external processing of that workbook is explicitly
within scope.
