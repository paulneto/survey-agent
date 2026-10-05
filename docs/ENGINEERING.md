# survey-agent: Engineering Runbook

How to deploy, test, operate, and extend the survey engine. Audience: whoever deploys and runs
survey-agent for their team, alongside their own panel, CRM, or email system.

## 1. Architecture

### 1.1 The model: a data-driven interpreter (not a scripting language, not code generation)

The most important thing to understand is that **there is one fixed engine that interprets surveys
as data.** It does not generate or deploy software per survey, and there is no embedded scripting
language. Here are three models you might expect, and which one this is:

| Model | How it works | survey-agent |
|---|---|---|
| Scripting language | you write code/expressions the tool executes | ❌ no |
| Code generation | builds/compiles a bespoke app per survey, then deploys it | ❌ no |
| **Interpreter over data** | one fixed program reads a survey *document* and runs it (like a browser running HTML, or a SQL engine running a query) | ✅ **yes** |

A survey is a **JSON document** (`surveys/<slug>.survey.json`). The engine reads it and runs it.
Authoring a new survey means writing a new JSON file: **no new code, no compile, no per-survey deploy.**
Ten thousand surveys are ten thousand JSON files run by the *same* engine.

**Logic is data, not code.** (Full reference, including every operator and the authoring gotchas:
**[LOGIC_IS_DATA.md](LOGIC_IS_DATA.md)**.) A rule is a tree of `{ref, operator, value}` nodes joined
by `and`/`or`:

```jsonc
{ "type": "disqualify", "reason_code": "non_user",
  "when": { "ref": {"kind":"question","id":"q_service_use"}, "operator":"equals", "value":"never" } }
```

The whole "logic language" is the recursive `evaluate(node, ctx)` function in `engine/ast_eval.py`
(about 15 lines). It walks that tree and returns a bool. The same function powers every condition in
every survey (show/hide, page logic, skip, disqualify, over-quota, routing). The engine has **no
`eval()` and no scripting**: operators are a closed set (`equals`, `contains`, `is_one_of`,
`greater_than`, `between`, `regex_match`, …). `validate` rejects regex patterns longer than 200
characters or with nested unbounded quantifiers, and input is matched up to 2,000 characters. You cannot express arbitrary code in a survey, by
design. An LLM can author the JSON; the JSON-Schema validator and semantic lint (`engine/validate.py`)
gate it before it runs.

**The one place code is generated** is the enrichment layer's reference warehouse adapter, which
compiles an enrichment *spec* (also data: `event_type + window + criteria + operation`) into a SQL
query (`enrich/firebolt_sql.py`). SQL identifiers are validated (a strict identifier pattern, plus
the catalog's field allow-list where one is listed) and values are parameterized. That's deterministic compilation of bounded data, not arbitrary code and not
per-survey software.

**Three layers** (what changes vs what's fixed):

```
DEFINITION (data, changes per survey)   surveys/*.survey.json — pages·questions·logic AST·piping·repeats·quotas·actions
        │  authored by people or agents · reviewed in a PR · validated · immutable + versioned
        ▼
ENGINE (fixed interpreter, ~2,600 lines) engine/ — never changes per survey; covered by the test suite
        │  runtime.py (page-flow state machine) · ast_eval.py (logic) · piping.py · repeat.py · randomize.py · validate.py
        ▼
RUNTIME — the stdlib `survey-agent serve`  (one long-running service; scale by replicas with a shared store)
        │  holds session state · renders pages · fires passbacks
```

When a respondent opens a `survey_url`, the runtime loads that survey's JSON and interprets it
page by page, the way a browser loads and renders an HTML file. Agents author the data, never the
engine code. The risky part (the interpreter) is written and tested once, while surveys stay cheap,
analyzable, diffable documents.

The engine is about 2,600 lines of pure Python with no dependencies beyond the standard library plus
`jsonschema` and `PyYAML`.

### 1.2 What runs where

survey-agent is one Python package with several roles, chosen by command. A typical deployment:

```
respondent ──(survey_url)──▶ respondent runtime  `survey-agent serve --public --multi`
                                  │  engine (logic) · store · enrichment provider · themes
                                  │  on start/terminal → passback via SURVEY_INTEGRATION
                                  ▼
                              your system (panel / CRM / webhook receiver)

operator / agent ──▶ API gateway `survey-agent api`   control plane: definitions, lifecycle, response reads
                  ──▶ operator console `survey-agent console` (local, 127.0.0.1)
                  ──▶ MCP connector `survey-agent-mcp`
trigger sidecar (optional) ── watches your behavioral data ── POSTs a launch request to PANEL_LAUNCH_URL
store (file / sqlite / mongo / http / your own) ── sessions · quota counters · responses
```

- **The respondent runtime** (`survey-agent serve`) is the hot, public path. `--public` binds
  0.0.0.0 and serves only `live` surveys, with the configured store and integration (defaults `file`
  and `none`); `--preview` serves drafts too, on localhost only.
- **The API gateway** (`survey-agent api`, the `[api]` extra) is the control plane: definitions,
  lifecycle, and read-side response access over HTTP. **It runs open (no auth) unless you configure
  auth**: a bearer `SURVEY_API_TOKEN`, Google OAuth (the `[auth]` extra, `SURVEY_OAUTH_*`), or `sa_`
  API keys. Never expose it unauthenticated.
- **The engine (`engine/`) is pure Python**, with no database, network, or framework imports. It is
  the correctness core and is reused by the runtime, the CLI (`report`/`insights`/`export`/
  `dashboard --live` read the local `SURVEY_STORE`), the simulator, the MCP tools, and the demo.
- **Definitions are immutable and versioned.** In-flight sessions pin their version, so an edit never
  corrupts a respondent's session or rewrites history.

### 1.3 Modules & extension points

The core is `engine/` (pure, offline). Everything else plugs in via selectors:

| Area | Module(s) | Registry / selector |
|---|---|---|
| Logic + calc | `engine/ast_eval.py`, `engine/calc.py` | (data) |
| Repeating | `engine/repeat.py`: `repeat` on a page or question; each item's answer is an ordinary answer keyed `qid[item]`, so logic/piping/quotas/export need no special case | (data) |
| Hidden values / scoring | `hidden` block → `engine/calc.py` → exported `derived` | (data) |
| Identity vars | which URL params are the respondent/response id (keys the session, dedup, enrichment, erasure) | `settings.identity` (`respondent`/`response`; defaults `uid`/`rid`) |
| Storage | `store/` (`memory`/`file`/`sqlite`/`mongo`/`http`/`your.pkg:Store`); default `file`; `mongo` is a reference backend | `SURVEY_STORE` |
| Definitions + lifecycle | `definitions/` (`file`/`mongo`/`http`/import path): serve specs + status from files or a runtime store; a runtime store makes publish and pause/close store writes (no redeploy), version-pinned | `SURVEY_DEFINITION_SOURCE` |
| API gateway (front door) | `api/` (FastAPI, `[api]` extra): exposes the definitions + read-side store seams over HTTP. Clients (console/agent/CLI) set `…=http` and become stdlib HTTP clients (no extra dependency) instead of opening their own DB connection. The respondent runtime keeps a direct store (hot path). | `SURVEY_API_BASE`, `SURVEY_API_TOKEN` |
| Serving gate (lifecycle) | the runtime serves respondents only when state ∈ serve-states (default `live`); `paused`/`closed` show their pages, other pre-live states show "not open yet"; operators preview pre-live surveys with a token (preview sessions collect nothing). Marking live pins `live_version`. A survey with no registry state (single-file `serve`) is unmanaged and served as before | `SURVEY_SERVE_STATES`, `SURVEY_PREVIEW_TOKEN` |
| Launch gate | one rule for every surface: with a token set, marking live needs it; unset, the local CLI/console may mark live and the MCP connector may not | `SURVEY_LAUNCH_APPROVAL_TOKEN` |
| Uploads (blob store) | `store/uploads.py` (`local`/`s3`): bytes of `file` answers; the response keeps only a reference | `SURVEY_UPLOAD_STORE` |
| Context sources | `enrich/`: `static` (fixtures) and the reference warehouse adapter; `source: url_var`; `source: llm` (completion-time analysis) | `SURVEY_ENRICH`, per-spec `source` |
| Integrations (passback) | `integrations/`: `none`/`redirect`/`webhook`/`panel`/`mailchimp` + your plugin; `panel` and `mailchimp` are reference examples of the seam | `SURVEY_INTEGRATION` |
| Webhook-action outbox | `integrations/outbox.py`: durable retry + dead-letter for mid-survey `webhook` actions (not terminal passbacks) | `SURVEY_OUTBOX`, `survey-agent outbox` |
| Themes | `themes/` (base + `dark`, `ocean`, `paper`) | `settings.theme`, `SURVEY_THEMES_DIR` |
| Templates | `templates/` (core) + entry-point plugins + `./templates` / `SURVEY_TEMPLATES_DIR` | `survey-agent templates` / `new` |
| LLM client | `ai.py` (`StubLLM` offline / Anthropic / OpenAI) | `SURVEY_LLM`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` |
| Conversational mode | `conversation.py` | front end over the same runtime |
| Consent/disclosure | `consent.py` (default AI notice/consent text) | `settings.ai` |

Any panel/vendor-specific passback belongs behind the `integrations` seam (selected by
`SURVEY_INTEGRATION` or shipped as an entry-point plugin), so `engine/` has no panel or vendor
coupling. LLM features that see *respondent* data (analysis enrichment and conversational mode) are
opt-in and audited; see §1.4 and [DATA_SECURITY.md](DATA_SECURITY.md).

### 1.4 LLM use: two boundaries
- **Authoring LLM** (skills, MCP, `survey-agent author`): sees survey **definitions only**, no
  respondent data.
- **Analysis LLM** (`source: llm` enrichment and conversational mode): sees declared **respondent**
  fields. `source: llm` analysis is **fail-closed**: it runs only when `SURVEY_LLM_ENRICH=1` **and** the
  spec is `opt_in: true` (plus optional `requires_consent`). It runs **at completion**, its result is
  stored on the response (exported under `derived`), and it does not drive live logic, piping, or
  routing. **Every call is recorded** on the response (`llm_audit`).

## 2. Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm,mongo,api]"

survey-agent demo                              # runtime on :8000 + mock receiver on :9000; writes ./survey_data
SURVEY_STORE=sqlite survey-agent demo --quota 2   # pick a store with the env var; tiny quota to hit over-quota fast

# the respondent runtime (multi-survey) against a local Mongo:
SURVEY_STORE=mongo SURVEY_MONGODB_URI=mongodb://localhost:27017 survey-agent serve --multi
# and/or the control-plane API gateway (bearer-token auth):
SURVEY_DEFINITION_SOURCE=mongo SURVEY_STORE=mongo SURVEY_MONGODB_URI=mongodb://localhost:27017 \
  SURVEY_API_TOKEN=dev survey-agent api
```

The demo uses the default `file` store, so responses land in `./survey_data` (override with
`SURVEY_DATA_DIR`). Use `SURVEY_STORE=memory` if you want nothing written to disk.

## 3. Testing & verification gates

```bash
pytest -q
survey-agent validate surveys/<slug>.survey.json
survey-agent compile surveys/<slug>.md
survey-agent simulate surveys/<slug>.survey.json --n 500
python -m survey_agent.sidecar.demo
```
CI should run all of the above; the validator and the simulator's coverage gate must pass before a
survey publishes. A passing gate ends with:

```
✅ coverage gate passed: complete + every declared screen-out reachable; all callbacks mapped.
```

`survey-agent selftest` runs a quick built-in smoke check of an install.

## 4. Configuration & secrets

Configuration is env-driven; keep secrets in your platform's secret manager. Key variables (see
`.env.example` for the full list):

- `SURVEY_STORE` (memory | file | sqlite | mongo | http | import path; **default `file`**),
  `SURVEY_DATA_DIR`, `SURVEY_MONGODB_URI` / `SURVEY_MONGODB_DB`.
- `SURVEY_DEFINITION_SOURCE` (file | mongo | http | import path; default `file`).
- `SURVEY_INTEGRATION` (none | redirect | webhook | panel | mailchimp | import path; **default
  `none`**), plus its settings: `SURVEY_WEBHOOK_URL` / `SURVEY_WEBHOOK_SECRET`, or the generic panel
  passback's per-event URLs (`SURVEY_PANEL_STARTED_URL`, `SURVEY_PANEL_COMPLETED_URL`,
  `SURVEY_PANEL_TERMINATED_URL`, `SURVEY_PANEL_TERMINATED_BY_USER_URL`) and
  `SURVEY_PANEL_SHARED_SECRET`, which signs those passbacks.
- `SURVEY_URL_SECRET`: signs survey URLs (`survey-agent sign-url <survey> uid=… rid=…`); the runtime
  verifies signatures when a survey sets `settings.url_signing.required`.
- `SURVEY_ENRICH` (`static` | the reference warehouse adapter | import path).
- `SURVEY_LAUNCH_APPROVAL_TOKEN`, `SURVEY_PREVIEW_TOKEN`, `SURVEY_SERVE_STATES`.
- `SURVEY_PUBLIC_BASE_URL` (optional): the runtime derives its public URL from common hosts
  (`RENDER_EXTERNAL_URL`, `FLY_APP_NAME`, `RAILWAY_PUBLIC_DOMAIN`, …); set this to override.
- LLM keys (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) only where authoring or opt-in analysis runs.

The stdlib runtime plus the reference Mongo store installs as a lean extra:
`pip install ".[mongo]"` (just `pymongo`).

Beyond env, the CLI reads `survey-agent.config.yaml`. The keys it honours are `store.backend`,
`integration`, `theme`, `output.dir` (where generated reports/dashboards/exports go, default
`reports/`; `$SURVEY_OUTPUT_DIR` also works and an explicit `--out` always wins), and
`deploy.command` (run by `survey-agent launch --deploy`; or `$SURVEY_DEPLOY_COMMAND`). Env vars win
over the file. `survey-agent doctor` reports the resolved install and config (including the resolved
public URL), runs deployment checks, and flags the multi-clone editable-install trap (auto-warned on
stderr; silence with `SURVEY_AGENT_NO_DOCTOR=1`).

## 5. Storage modes
`store/` is pluggable (`SURVEY_STORE`). Treat it as a ladder and climb only as far as your deployment
needs:
- **memory**: tests and throwaway runs only (lost on restart).
- **file** (default): flat JSONL responses, JSON sessions, and quota files under `SURVEY_DATA_DIR`
  (default `./survey_data`). Inspectable, easy to back up or sync, zero infrastructure. File-locked
  quota increments are safe in a **single process** only.
- **sqlite**: one SQLite file, with quota increments that are **atomic and safe across threads and
  processes** (WAL mode). Still pure stdlib, no server. The step up from `file` for multiple workers
  on one box. Path via `SURVEY_SQLITE_PATH`. (Not for multiple *hosts*: SQLite over NFS is unsafe.)
- **mongo**: a **reference** multi-host backend (one option, not a requirement). Idempotent session
  creation (unique `(survey_id, rid, uid)`), atomic quota increments, indexed uid lookup. Read it as a
  worked example of the store contract.
- **http**: the store seam over the API gateway, for clients (console, CLI, agents) that shouldn't
  hold a database connection.
- **bring your own**: set `SURVEY_STORE=your.pkg:YourStore` and implement the store contract (the
  methods listed in `store/__init__.py`, e.g. `get_or_create_session`, `incr_quota`,
  `record_response`, `delete_responses`) on Postgres, DynamoDB, Redis, or your warehouse. For
  multiple hosts, use this or `mongo`.

Every store implements `delete_responses`, so `survey-agent dsar <uid> [--survey ID] --delete` finds
and erases a subject's responses uniformly. Backups, exports, and any copies in your downstream
systems remain your responsibility.

> With the default `file` definition source, definitions are files in git; with a runtime definition
> store (`SURVEY_DEFINITION_SOURCE=mongo` or `http`) they're published to the store (version-pinned,
> no redeploy) and the local files stay the authored artifact and backup. Two reference Render
> blueprints ship for the multi-survey runtime: `render.yaml` (file store on a persistent disk; simple
> and low-traffic, **not** zero-downtime, since a disk binds to one instance) and `render.mongo.yaml`
> (diskless + Mongo; rolling deploys). Both blueprints default to Render's paid `starter` plan; the
> file blueprint needs a paid instance for its persistent disk (as of 2026-10). See
> [DEPLOY.md](DEPLOY.md).

## 6. Behavioral enrichment (your own data)
`enrich/` resolves declarative specs (`event_type` + `window` + `criteria` + `operation`) into values
that a survey can use in logic, piping (`{{ctx:…}}`), and routing.
- **Provider** via `SURVEY_ENRICH`: `static` (in-Python over fixtures; for demos and tests) and a
  **reference warehouse adapter** (`firebolt`) that compiles one tight query per spec (short timeout,
  cache-first with write-back). To use your own data source (Postgres, BigQuery, Snowflake,
  ClickHouse, an HTTP profile service, a feature store), **write an adapter** that implements the
  provider interface in `enrich/__init__.py` and select it with `SURVEY_ENRICH=your.pkg:Provider`.
  HTTP, database, geo-IP, user-agent, and prior-response sources are not built in.
- **Speed:** prefetch fires on **session start** (only for respondents who actually begin), warming
  the cache while the intro page renders; `resolve` reads cache-first. For the lowest latency, back
  enrichments with a precomputed per-respondent feature store and let the warehouse be the cache-miss
  fallback.
- **Respondent id column:** the reference adapter looks respondents up by `respondent_id` by default;
  override it per event type with `id_field` in the catalog, or globally with `SURVEY_ENRICH_ID_FIELD`.
- **Event-type catalog:** `enrich/event_types.json` maps each event type (the example catalog has
  `web_search`, `app_usage`, `page_view`, and `purchase`) to a table and fields. Extend it for your own data. Never let an LLM author raw SQL: specs
  are data, compiled to validated identifiers and parameterized values here.

## 7. Trigger sidecar
`sidecar/` watches your behavioral data and offers a survey when a behavior matches.
- Runs as a small long-lived process (`python -m survey_agent.sidecar.runner`), polling on a
  watermark. Triggers are JSON files (see `triggers/` in the repo; directory via `SURVEY_TRIGGERS_DIR`).
- The detector compiles a distinct-respondent query (incremental on `event_time`). Cooldown and
  throttle prevent re-offers and spam.
- On a match it sends one JSON `POST` to `PANEL_LAUNCH_URL` (`sidecar/panel_client.py`), with
  `Authorization: Bearer $PANEL_LAUNCH_TOKEN` if set and an HMAC `X-Survey-Signature` /
  `X-Survey-Timestamp` if `PANEL_LAUNCH_SECRET` is set. The body is
  `{type: "survey.launch_request", version: 1, idempotency_key, trigger_id, survey_id, survey_url,
  respondent_ids[], audience, …}` (`survey_url` keeps its `{uid}`/`{rid}` placeholders; optional
  `target_completes`, `budget`, and `metadata` come from the trigger's `launch` block). Any JSON
  response is accepted (a `launch_id` is recorded if present); a non-2xx response is retried on the
  next poll. Your system decides how to invite those people, and outcomes come back through the
  integration seam as the usual lifecycle events.
- Surveys are **pre-approved** (referenced by id); personalization is per respondent via enrichment.
  New trigger surveys go through author → test → launch with a human gate first.
- `python -m survey_agent.sidecar.demo` runs the loop end to end offline.

## 8. Deploy

**Start simple.** The repo ships a single `Dockerfile` that runs the stdlib runtime
(`survey-agent serve --public --multi`) on `:8000` and deploys as-is to any container host. For
low-cost options (Cloud Run, Render, Fly, Railway, and others), storage choices, and where to host the
remote MCP connector, see **[HOSTING.md](HOSTING.md)** and **[DEPLOY.md](DEPLOY.md)**.

**One larger option: AWS ECS (illustrative skeleton).** `terraform/ecs/main.tf` sketches one shape:
separate services for the runtime, API gateway, and sidecar, each deployed with
`build → push to ECR → ecs update-service --force-new-deployment → wait stable`. It covers ECR,
cluster, ALB host routing (a public survey host vs an internal admin host), IAM (LLM access only on
the API service), and request-count autoscaling on the runtime. Fill in VPC/subnets/certificates and
the task-definition/service/security-group blocks. Any other container platform works the same way.

## 9. Monitoring & on-call
Alarm on, at minimum:
- **Passback failures.** A dropped passback means your system never hears that someone completed or
  screened out (missing records, miscounted quotas downstream). The built-in integrations make a
  single best-effort attempt per event, each carrying a stable `idempotency_key`, so your receiver
  can dedupe safely. Watch delivery errors in the runtime logs and reconcile against the store
  (`survey-agent export`) when needed.
- **Webhook-action outbox dead-letters** (if `SURVEY_OUTBOX=1`): `survey-agent outbox status`,
  `outbox list [--dead]`, and `outbox retry` inspect and drain pending and dead-lettered mid-survey
  webhook actions (set `SURVEY_OUTBOX_RETRY_SECONDS` for a background sweep). Retries are safe
  because each intent has a stable id.
- Runtime 5xx rates; database CPU and connections; LLM provider throttling where authoring/analysis runs.
- Correlate logs by response id (`rid` by default).

**Common ops:**
- *Survey logic wrong after launch:* edit, cut a new version, re-publish; in-flight respondents are
  unaffected.
- *Passbacks not landing:* check `SURVEY_INTEGRATION`, the integration's URLs, and its shared secret
  (`survey-agent doctor` shows the resolved config).
- *Over-quota firing early:* check the store's quota counters against the survey's limits; confirm
  the quota increments on complete.
- *Enrichment slow or empty:* check data-source latency and the cache; confirm the spec's
  window/criteria; gate behavior pages so they no-op when the fact is absent.

## 10. Migrating a survey from another tool (per survey, reversible)
1. Rebuild the existing survey as a `*.survey.json` (LLM-assisted from its export); validate and simulate.
2. Publish it, and point the link your system hands out at survey-agent's `survey_url`.
3. Soak side by side with the old copy, comparing complete and screen-out rates.
4. Retire the old survey. The documented **export shape** (below) means your downstream data and
   erasure tooling needs only a base-URL/auth swap.

## 11. The drop-in contract (do not break)
- `survey_url` captures **all** query params into `url_variables`.
- Lifecycle events are neutral: `started`, `completed`, `terminated` (disqualified or over-quota),
  `terminated_by_user`. Each event carries
  `{event, status, reason_code, survey_id, response_id, url_variables, idempotency_key}`; the selected
  integration maps it to your system's wire format. (The stdlib runtime has no abandon detection, so it
  never emits `terminated_by_user` itself; a plugin or host can.)
- `webhook` **actions** fire once per session when their condition is true; the engine returns them as
  intents and the runtime delivers them (durably with `SURVEY_OUTBOX=1`).
- Export response shape: `{id, date_started, date_submitted, status, url_variables,
  survey_data{qid:{id,question,answer}}, derived{…}}`, where `id` is an opaque system-generated
  response id and `status` is a word such as `"Complete"`. A repeated question adds one entry per
  item, keyed `qid[item]`, with an extra `repeat: {source, value, label}` (additive; existing consumers
  ignore it). Exports are not redacted.
