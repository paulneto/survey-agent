# CLAUDE.md — survey-agent

Guidance for Claude Code working in this repo.

## What this is
An open-source, agentic survey engine. A survey is a **JSON/Markdown document**; one small, fixed,
pure-Python engine *interprets* it (like a browser runs HTML). It emits a drop-in `survey_url`, runs
question logic, captures answers, and — via a pluggable integration — can pass results back to a
panel/CRM/webhook. Sampling, targeting, quotas across a panel, and incentive payment are **out of
scope** — they belong to whatever panel adapter a deployer plugs into the `integrations` seam. See
`README.md` and `docs/ENGINEERING.md`.

## Architecture rules (important)
- **`survey_agent.engine` is pure Python.** No Mongo/network/framework imports. It's the correctness
  core, reused by the runtime, simulator, MCP, and demo. Keep it that way; put I/O in `store/`,
  `enrich/`, `integrations/`, and the API gateway (`api/`).
- **Logic is data, never code.** Conditions are the AST in `engine/ast_eval.py` — no `eval()`. An LLM
  authors specs; the validator (`engine/validate.py`) + JSON Schema (`engine/schema/survey.schema.json`)
  are the gate.
- **Surveys/themes/enrichments/triggers are flat files.** Add a file + a small hook; don't build CMS UI.
- **Definitions are immutable + versioned.** Edits create a new version; in-flight sessions stay pinned.
  The `file` source keeps snapshots in `surveys/.versions/<id>/v<N>.survey.json` — commit them.
- **Saving is not approving.** No backend's `put_definition` moves the registry's `live_version`; only
  an approval-gated mark-live does (and with no explicit version it pins the newest saved one).
  `get_registry` reports `live_version` and `latest_version` separately. Keep it that way.
- **Server-driven page flow:** a question that depends on another answer goes on a *later page*
  (`validate` rejects a same-page or later-page dependency). The same holds for a `repeat` source.
- **Repeated answers are ordinary answers.** A `repeat` stores each item's answer as `qid[item]`
  (`engine/repeat.py`), so logic, piping, quotas, and export need no special case. Keep it that way.
- **Display-only types** (`image`, `video`, `instructions`) are stimulus — never required, never validated.

## Don't break the drop-in contract
- `survey_url` captures **all** query params into `url_variables` (stored on the response).
- Terminals produce neutral lifecycle events (`engine/terminal.py`); the selected integration
  (`SURVEY_INTEGRATION`: `none`/`redirect`/`webhook`/plugin) delivers them with an idempotency key.
  An event's `url_variables` carries only the declared `settings.url_variables` + identity vars.
- The export shape (`engine/export.py`) is stable and documented so DSAR / data-export tooling keeps
  working with only a base-URL/auth swap. Repeated answers add one `qid[item]` entry each, with an
  extra `repeat: {source, value, label}` — additive, so existing consumers are unaffected.
- The `webhook` **action** is fire-once per session and network-free in the engine: `submit()` returns
  resolved intents under `result["webhooks"]`; the caller delivers them via `integrations.webhook.deliver`.
- Terminal processing (`server.on_terminal`) is retry-safe: persist the intent → `reserve_quotas(...,
  key=response_id)` → `record_response` (an upsert by response id) → mark recorded → best-effort
  integration call (a failure is logged, never shown to the respondent). Stores must keep both
  `reserve_quotas` idempotent per key and `record_response` an upsert.
- Signed links fail closed: a survey with `settings.url_signing.required` refuses every invitation
  while `SURVEY_URL_SECRET` is unset or blank.

## Code layout (packaged under `src/survey_agent/`)
Importable modules live in `src/survey_agent/` (namespace package; installed via `pip install -e .`).
The stdlib runtime is `server.py`; the optional authenticated control plane is the API gateway under
`api/` (the `[api]` extra; it replaced the old Django reference service, which has been removed).
`surveys/`, `tests/`, `terraform/`, and `skills/` stay at the repo root. The `survey-agent` console
script is the entry point for everyday tasks (`survey-agent help` prints a guided overview of the
commands + the flow).

## Workflow
- After any engine change: `pytest -q` (the full suite) must stay green.
- After editing a survey: `survey-agent validate surveys/<slug>.survey.json` and keep `.md` ⇄ `.json`
  in sync (`survey-agent compile surveys/<slug>.md`).
- Before "launch": `survey-agent simulate surveys/<slug>.survey.json --n 500` coverage gate.
- Try changes live: `survey-agent demo` (runtime on :8000 + a mock receiver on :9000; responses go to
  `./survey_data`), `survey-agent serve --preview` for a local draft preview, or
  `survey-agent convo --scripted …` for the conversational mode.

## Conventions
- Pure-engine deps are tiny (`jsonschema` + `pyyaml` + `regex`, the last for timeout-bounded pattern
  matching); everything else is an extra: `[llm]` (Anthropic), `[openai]` (GPT), `[mcp]` (connector),
  `[api]` (the gateway: FastAPI/uvicorn), `[auth]` (gateway Google OAuth sign-in + the `/keys` page;
  install as `[api,auth]`), `[mongo]` (the reference Mongo store), `[firebolt]` (the reference
  warehouse-enrichment adapter), `[s3]` (S3 blob store for `file` uploads), `[data]` (parquet export).
  The `sqlite` store is built in (stdlib — no extra).
- Storage via `SURVEY_STORE` (memory|file|sqlite|mongo|http|`your.pkg:Store`; unset = `file`);
  enrichment via `SURVEY_ENRICH` (static|firebolt|`your.pkg:Provider`); passback via
  `SURVEY_INTEGRATION` (none|redirect|webhook|panel|mailchimp|your plugin; unset = `none`). These are **seams**: mongo, firebolt, and
  mailchimp ship as reference examples, not requirements — a deployer brings their own. Config via
  `survey-agent.config.yaml` + env. `survey-agent doctor` reports the resolved install + config,
  runs deployment checks, and flags the multi-clone editable-install trap.
- Skills live in `skills/`; the authoring loop is brief → draft → validate → simulate → human approve
  → launch → report. **The human approval gate before launch is mandatory** (launching fields to real
  people and spends budget). When `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface can mark a survey
  live without it; when unset, the local CLI/console can and the MCP connector / API gateway cannot.
- Keep this repo free of any deployer-specific (panel/vendor) names and panel vocabulary — those
  belong in a private overlay/plugin, not the open core.
- Docs: `docs/README.md` is the map and `src/survey_agent/docs.py` mirrors it for `survey-agent docs`;
  `tests/test_docs.py` checks the map and every relative link/anchor in the Markdown docs.
