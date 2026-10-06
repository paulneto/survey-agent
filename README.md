# survey-agent

**The agentic survey engine.** Describe a survey in plain English. An agent drafts it, stress-tests it
on hundreds of simulated respondents, fixes its own logic, then runs it as a form or a conversation.
You own the data, it runs anywhere, and you bring your own LLM.

> Status: alpha (0.1.0). Apache-2.0 licensed. Repo:
> [github.com/paulneto/survey-agent](https://github.com/paulneto/survey-agent)

> **Driving this as an LLM/agent? Start with
> [AGENTS.md](https://github.com/paulneto/survey-agent/blob/main/AGENTS.md).** One line to add the MCP
> connector: `claude mcp add survey-agent -- uvx --from "survey-agent[mcp,llm]" survey-agent-mcp`.
> Or jump to [Use it inside your LLM](#use-it-inside-your-llm-file-access-surfaces). Self-hosting?
> [docs/HOSTING.md](https://github.com/paulneto/survey-agent/blob/main/docs/HOSTING.md) helps you
> decide what your deployment actually needs.

> **Install note:** the PyPI package ships with the first release. Until then, install from GitHub:
> `pip install "git+https://github.com/paulneto/survey-agent"` (with extras:
> `pip install "survey-agent[llm] @ git+https://github.com/paulneto/survey-agent"`), and for `uvx`
> use `--from "survey-agent[mcp,llm] @ git+https://github.com/paulneto/survey-agent"`.

survey-agent makes a survey something you operate by talking to an AI. A survey is a single file; a
small, fixed engine runs it. Point a file-access agent (Claude Code, or the Claude desktop app /
Cowork) at a folder of surveys and it can draft, test, launch and report on them, with the engine
checking every step.

- **Agentic.** Describe what you want; an agent drafts, validates, simulates, launches (after you
  approve), and reports. You drive it from a file-access agent surface via the bundled
  [`skills/`](https://github.com/paulneto/survey-agent/tree/main/skills) and an
  [MCP connector](https://github.com/paulneto/survey-agent/blob/main/src/survey_agent/mcp/server.py),
  or with one command: `survey-agent author "…"`. A survey is a file the agent reads, writes, and
  tests, so a file-access surface is required to build one; a chat-only surface can take a survey but
  not build one. See [Surfaces](#use-it-inside-your-llm-file-access-surfaces).
- **The agent tests its own work.** A pure-Python engine and a coverage simulator give the agent a
  ground-truth feedback loop: it runs hundreds of synthetic respondents, sees where the logic breaks,
  and fixes it. A one-shot "ask an LLM for a survey JSON" can't do that. At field time the engine,
  not the model, runs the logic. No `eval`, ever.
- **One spec, two delivery modes.** The same survey runs as a web form or an AI conversation. The
  form renders every question type. The conversation handles choice, number and text questions;
  grids, rankings and file uploads aren't supported in conversation, so use the form for those. It
  reads naturally only with your own LLM key (the offline stub is deliberately mechanical).
- **Logic is data.** Conditions are a bounded JSON AST, so a draft can be validated and simulated
  before it fields, and a bad LLM draft can only produce a wrong survey, never code that runs. See
  **[docs/LOGIC_IS_DATA.md](https://github.com/paulneto/survey-agent/blob/main/docs/LOGIC_IS_DATA.md)**
  for the design decision in full, or
  [the architecture](https://github.com/paulneto/survey-agent/blob/main/docs/ENGINEERING.md#1-architecture).
- **You own everything, and it runs anywhere.** Flat files or a database, a container or a serverless
  function, a drop-in `survey_url` for any panel, CRM or email. The engine is about 2,700 lines of pure
  Python with three dependencies (`jsonschema`, `PyYAML`, `regex`). No per-response license, no seats.
  Bring your own Claude or OpenAI key.

### Who it's for

One workflow, two roles, often the same small team and sometimes the same person:

- **Set up once (the technical role).** Someone who can run a terminal stands up the few things that
  have to live somewhere durable: a publicly reachable runtime for the `survey_url` (a small
  scale-to-zero host, not a laptop; see
  [docs/HOSTING.md](https://github.com/paulneto/survey-agent/blob/main/docs/HOSTING.md)), a response
  store, and an erasure path. See
  [docs/ENGINEERING.md](https://github.com/paulneto/survey-agent/blob/main/docs/ENGINEERING.md) and the
  [operator responsibilities](https://github.com/paulneto/survey-agent/blob/main/docs/OPERATOR_RESPONSIBILITIES.md).
- **Operate continuously (the research role).** Against that instance, a researcher works by talking
  to an agent: describe a survey in plain language, review a readable `.md` diff, simulate it,
  approve, launch, and read the results. No JSON by hand. Authoring and testing are fully local and
  offline; only fielding uses the hosted runtime.

There's also a distinct third entry point:

- **Agent builders** who need a human-in-the-loop structured-elicitation primitive. `begin_survey` and
  `answer_survey` collect clean, logic-gated answers from a person mid-task. See
  [docs/PRIMITIVE.md](https://github.com/paulneto/survey-agent/blob/main/docs/PRIMITIVE.md).

## Key features

### The agentic loop

- **Authoring that checks itself.** Author from a plain-language brief; each draft is repaired until
  it passes validation, the coverage gate and, when the brief states them, incidence and length
  targets. Persona pre-test and an LLM methodology critique run before launch. Deliver the survey as
  an AI conversation (with optional follow-up probing on open text) or as an inline agent tool
  (`begin_survey`). Sending respondent data to an LLM is opt-in and disclosed in the consent text.
- **Reporting, insights, and export.** `survey-agent report` writes a self-contained HTML report
  (funnel, incidence, length of interview, per-question charts, NPS, segments, quota burndown). Add
  `--insights` (or run `survey-agent insights`) for an AI analyst read-out: a headline, key findings,
  open-end themes, watch-outs, and suggested follow-up questions. `survey-agent export` writes a CSV or
  Parquet response grid. All of these work on live data or a quick simulation.
- **Manage a series of surveys.** `survey-agent list` is the index of every survey in a project: its
  purpose (`description`, `tags`, `owner`, which travel with the file), its lifecycle state
  (draft → validated → tested → approved → live → paused / closed), and the last test's incidence and
  length. State is tracked as you author, validate, and simulate, and recorded in a `.registry.json`
  sidecar so it never mutates an immutable definition. **`live` is enforced:** a production runtime
  (`serve --public`) serves respondents only when a survey is `live` (configurable via
  `SURVEY_SERVE_STATES`); other surveys show a "not open yet" page. Preview drafts locally with
  `survey-agent serve --preview`. Group surveys into projects with `status <id> --project "<name>"`.
  The same view is available through the MCP `list_surveys` tool and the `survey://catalog` resource.
  `survey-agent dashboard` writes a self-contained HTML grid; **`dashboard --live`** adds each survey's
  drop-in URL, accepted params, and live counts from the store.
- **Operator console.** `survey-agent console` is a local GUI onto the same engine actions: a board of
  every survey (grouped into projects, with status, completes and incidence), a read-only preview, the
  launch checks (validate / simulate), an AI read-out, the copyable drop-in URL, lifecycle buttons
  (pause / resume / close / mark live), response download, and create-from-template. It binds
  `127.0.0.1` and has no authentication of its own; if you host it for a team, put your own auth in
  front of it.
- **Contextual enrichment.** Look up a fact about the respondent when the session starts and use it in
  logic, piping, or routing. Ships with a fixture provider for demos and tests and a reference
  warehouse adapter; for any other source, you write a small provider.

### Delivery and ownership

- **Drop-in `survey_url`.** It captures every query parameter into `url_variables`. Identity variables
  are configurable (`settings.identity` sets `respondent` and `response`; they default to `uid` and
  `rid`), so you can use whatever names your panel or CRM already sends.
- **Runs and integrates anywhere.** Storage is a thin seam and a short ladder: flat `file` by default
  (no infrastructure, git/S3-syncable) → built-in `sqlite` (one file, safe for several workers on one
  box) → a database when storage spans hosts. For a database, bring your own
  (`SURVEY_STORE=your_pkg:YourStore`) or use the reference `mongo` backend. Completion integrations
  (`none` by default, `redirect` browser passback, `webhook`, a generic `panel` passback, a reference
  `mailchimp` adapter, or your own plugin) and themes plug in the same way.
- **Built for trust and ops.** Definitions are immutable and versioned. Webhooks are HMAC-signed, and
  URL params can be too: sign `uid`/`rid`/`group` so eligibility can't be edited in the address bar
  (mint links with `survey-agent sign-url`; the runtime verifies them when
  `settings.url_signing.required` is set). Launch is human-gated by an approval token (see
  [Use it inside your LLM](#use-it-inside-your-llm-file-access-surfaces)). A per-survey
  `settings.retake` (`once` by default, or `multiple`) controls whether one invite link can be taken
  again. `survey-agent dsar <id>` finds everything the app holds for a respondent (responses and
  sessions, on every identity variable in use) and `--delete` erases it, including their uploaded
  files and queued webhook deliveries; copies already sent or exported elsewhere remain yours to
  handle.

### Survey mechanics the engine guarantees

- **Question types.** Radio, checkbox (with an exclusive "none of the above"), dropdown and
  multi-select dropdown, short text (regex / email / phone / length / minimum words), long text,
  number (min / max / percent), rating / Likert, NPS, ranking, grid / matrix (radio or checkbox
  cells), date, and file upload (extension / size / count validated before storing, kept in a
  pluggable local or S3 blob store). Display-only image, video and instructions blocks render
  formatted Markdown. They all
  render, validate, simulate, and export. See the
  [capability matrix](https://github.com/paulneto/survey-agent/blob/main/docs/FEATURE_COVERAGE.md).
- **Logic, as data.** Show/hide, page display, skip/branch, disqualify/screen-out, per-option and
  grid-row logic, nested AND/OR, and a closed operator set (`equals`, `is_one_of`, `greater_than`,
  `between`, `regex_match`, and so on). No `eval`, no scripting.
- **Piping and merge tokens.** Reuse any answer, URL value, variable, or context fact (`{{q:…}}`,
  `{{url:…}}`, `{{var:…}}`, `{{ctx:…}}`), with fallbacks for blanks, plus option piping that carries
  selected options forward.
- **Question and page repeating.** Ask a question, or a whole page, once per item from an earlier
  answer: each brand someone picked (or didn't), or N times from a number. Each item's answer is its
  own key (`q_rate[netflix]`), so logic, piping, quotas, and export treat it like any other answer.
- **Randomization.** Seeded and deterministic per session, with locked positions to anchor "Other" and
  "None".
- **Quotas.** Overall and per-answer-option, enforced atomically.
- **Hidden values and calculations.** Scoring and segmentation through a calculation AST
  (score / sum / bucket / count / arithmetic), usable in logic, piping, quotas, and routing, and
  exported as `derived`.
- **Image and video stimulus.** Show an image (any URL, with size controls, `alt` and `caption`) or a
  video (a YouTube privacy-mode embed or a direct-URL clip) before a question.
- **Webhooks.** A conditional, once-per-session POST mid-survey (with a merge-token payload), plus a
  completion integration that POSTs lifecycle events to any endpoint.

## Quickstart

```bash
pip install "survey-agent[llm]"          # see the install note above; add ,mcp for the connector

survey-agent help                                    # a guided tour of the commands + the flow
survey-agent init my-surveys && cd my-surveys
survey-agent doctor                                  # verify the CLI runs THIS install (+ which extras/config)

survey-agent author "screen US adults who stream paid video, quota 300, route qualifiers to a task"
survey-agent list                                    # every survey + purpose + state (draft/tested/live)

survey-agent validate surveys/<slug>.survey.json
survey-agent simulate surveys/<slug>.survey.json -n 500   # the coverage gate
survey-agent pretest  surveys/<slug>.survey.json --n 6    # persona pre-test: incidence + length
survey-agent demo --survey surveys/<slug>.survey.json     # take it in the browser (writes ./survey_data)
survey-agent convo  --survey surveys/<slug>.survey.json   # take it as a conversation
survey-agent report surveys/<slug>.survey.json --simulate 500 --open
survey-agent export surveys/<slug>.survey.json --simulate 500
survey-agent dashboard --live --open                 # all surveys: drop-in URL + params + live counts
```

To hack on it:

```bash
git clone https://github.com/paulneto/survey-agent && cd survey-agent
pip install -e ".[dev,llm,mcp]" && pytest -q
```

### Then deploy the runtime once

Authoring, validating and simulating are fully local. **Fielding needs a publicly reachable runtime**,
because the drop-in `survey_url` has to open for a stranger. That's one deploy per instance, not per
survey. The Render blueprint below provisions a paid instance (a persistent disk needs one); Fly.io
can scale to zero.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/paulneto/survey-agent)

```bash
fly launch --copy-config --no-deploy && fly deploy    # Fly.io (fly.toml)
```

The runtime **derives its own public URL** from the host (`RENDER_EXTERNAL_URL`, `FLY_APP_NAME`,
`RAILWAY_PUBLIC_DOMAIN`, …), so your `survey_url` is correct once it's up; set
`SURVEY_PUBLIC_BASE_URL` only to override it. Check it with `survey-agent doctor`:

```
resolved survey base   : https://my-surveys.onrender.com  (via Render; reachable)
```

If that line reads `LOCAL ONLY — respondents cannot open it`, the URL reaches nobody, and `launch`
warns you when it hands it over. Options, trade-offs and storage rungs:
[docs/HOSTING.md](https://github.com/paulneto/survey-agent/blob/main/docs/HOSTING.md).

Deploying gives you a runtime, not an audience: sampling, targeting and incentives are out of scope by
design. You drop the `survey_url` into your own panel, CRM or email.

Full step-by-step walkthrough (install, validate, simulate, browser demo with live webhooks,
conversational mode):
[docs/QUICKSTART.md](https://github.com/paulneto/survey-agent/blob/main/docs/QUICKSTART.md). Want to
*try the whole lifecycle by talking to your agent*? Paste through
[demo-survey-agent.md](https://github.com/paulneto/survey-agent/blob/main/demo-survey-agent.md).

## See it in action

One sentence in. Every draft has to clear the gates. Schema is first. Coverage is next: the engine
drives synthetic respondents and checks that a qualified one reaches complete and that every declared
screen-out is reachable. When the brief states them, incidence and length are checked too. Any gap is
fed back to the model until it's fixed.

```text
$ survey-agent author "screen US adults who currently pay for streaming, incidence 50%, under 3 min"
authoring with: claude
target incidence: ~50% — each valid draft is persona pre-tested against it
target length: ~3 min — each valid draft's estimated LOI is checked
  draft 1: valid, but 1 gap(s) — repairing…
      • [incidence] a realistic persona pre-test qualified ~28% but the brief targets ~50%
        (too few qualify — loosen or remove a screen-out)
  draft 2: valid ✅  ·  ✅ coverage gate passed  (104/200 complete, 2/2 screen-outs firing)
           ·  persona incidence ~47% (target ~50%)  ·  ~2.4 min LOI
wrote surveys/streaming-screener.survey.json
```

That repair step, the agent reading why its survey failed on synthetic respondents and fixing it, is
what a one-shot "ask an LLM for a survey JSON" can't do. Then you preview who actually qualifies with
role-played persona respondents, before fielding to a single real person:

```text
$ survey-agent pretest surveys/streaming-screener.survey.json --n 5
  1. a heavy subscriber who pays for several streaming services   ✅ COMPLETED
  2. a retiree new to apps who cancels subscriptions often        ⛔ DISQUALIFIED · non_user
  3. a budget-conscious student who streams on a shared account   ✅ COMPLETED
Incidence (qualified): 3/5  (60%)
```

Run the whole loop yourself with
[`scripts/hero-demo.sh`](https://github.com/paulneto/survey-agent/blob/main/scripts/hero-demo.sh)
(author, pretest, report). It runs offline on a deterministic stub; add a Claude or OpenAI key and
you'll watch it self-correct on real coverage and incidence gaps.

## Use it inside your LLM (file-access surfaces)

A survey is a file the agent reads, writes, validates, and simulates, so survey-agent runs on the agent
surfaces that have file access. A chat surface with no filesystem can take a survey, not build one.
The two first-class surfaces:

| Surface | How you drive it | What it uses |
|---|---|---|
| **Claude Code** (VS Code extension or CLI) | the bundled [`skills/`](https://github.com/paulneto/survey-agent/tree/main/skills); shell and file tools present | the `survey-agent` CLI |
| **Claude desktop app (Cowork)** and other file-access agents | the **MCP connector** | typed tools over the same engine |

**Skills vs. the connector.** Skills are the authoring workflow in Claude Code, where they drive the
CLI directly: author-survey, edit-survey, add-logic, test-survey, launch-survey, survey-report and
inline-survey. The MCP connector is two things: the portable tool layer for Cowork and any
file-access agent that doesn't load `.claude/skills/`, and the agent-as-tool primitive described
below. Both are thin layers over the same engine, so pick by where you're working. For what each host
can and can't do, see
[docs/AGENT_HOSTS.md](https://github.com/paulneto/survey-agent/blob/main/docs/AGENT_HOSTS.md).

**Add the MCP connector.** One line in Claude Code:

```bash
claude mcp add survey-agent -- uvx --from "survey-agent[mcp,llm]" survey-agent-mcp
```

…or drop this into any file-access MCP client config (Cowork's `claude_desktop_config.json`,
Cursor's `.cursor/mcp.json`, …):

```jsonc
{
  "mcpServers": {
    "survey-agent": {
      "command": "uvx",
      "args": ["--from", "survey-agent[mcp,llm]", "survey-agent-mcp"],
      "env": { "SURVEY_DIR": "surveys", "ANTHROPIC_API_KEY": "sk-ant-…" }
    }
  }
}
```

It exposes the whole loop as tools: `save_survey` persists a draft as a managed file,
`validate_survey` and `critique_survey` check it, `simulate_survey` runs the coverage gate (on a
saved file or an inline draft), `pretest_survey` previews incidence, `compile_markdown_survey`
renders the readable form, `list_surveys` is the management view, and `survey_report` /
`export_survey` read results. The schema and catalog ship as resources, with an `author_survey`
prompt.

**The launch gate.** Marking a survey live is gated by an approval token that a human holds. When
`SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface (the CLI's `launch` / `status` / `publish`, the
console, the MCP connector, or the API gateway) can mark a survey live without it. When it is unset,
the local CLI and console can, and the MCP connector and API gateway still cannot.

**As an agent tool: the structured-elicitation primitive.** Any agent can call `begin_survey` and then
`answer_survey` to run a logic-gated survey inline, mid-task, and get back clean, validated
`survey_data` along with derived fields, status, and a screen-out reason. The engine enforces every
branch, quota, and screen-out; the calling agent only relays messages. See
[docs/PRIMITIVE.md](https://github.com/paulneto/survey-agent/blob/main/docs/PRIMITIVE.md).

Without any app at all, `survey-agent author "<brief>"` runs the same loop from one command, and
`survey-agent list` and `status` manage the set.

**Discover and publish.** A
[`server.json`](https://github.com/paulneto/survey-agent/blob/main/server.json) manifest ships for MCP
registries. For hosting a remote connector, see
[docs/HOSTING.md](https://github.com/paulneto/survey-agent/blob/main/docs/HOSTING.md).

## A survey is just data

Canonical form is JSON, authorable as friendlier Markdown that compiles to it 1:1. Logic is a small,
composable AST with the same shape everywhere (question and page display, skip, disqualify, quotas):

```jsonc
{ "type": "disqualify", "reason_code": "non_user",
  "when": { "ref": {"kind":"question","id":"q_use"}, "operator":"equals", "value":"never" } }
```

The engine reads that and runs it. Building a survey means writing a file; there's no compile step and
no software to ship per survey.

**Why logic is data.** The language is closed, with no scripting escape hatch, so every survey is
checked before it fields, and a bad LLM draft can only produce a wrong survey, never code that runs on
your server. One small evaluator handles every logic surface, and the same engine runs the web form,
the simulator, the conversation, and the exports, so what you simulated is what fields. Survey specs
are still trusted operator input: the gates make an LLM-authored spec safe to *run*, and a human
approves it before it reaches real people. New capabilities (repeating was the latest) land once in
the engine as vocabulary, and every survey can use them. The reasoning is in
**[docs/LOGIC_IS_DATA.md](https://github.com/paulneto/survey-agent/blob/main/docs/LOGIC_IS_DATA.md)**.

## Agentic modes ([docs/AGENTIC_MODES.md](https://github.com/paulneto/survey-agent/blob/main/docs/AGENTIC_MODES.md))

- **Authoring.** Brief → validated survey JSON → simulate → human approval → launch, via the Claude
  skills and the MCP connector. The authoring AI only ever sees survey definitions.
- **Conversational mode.** An AI administers the same survey as a chat. The engine stays the source
  of truth for flow, branching, screen-out, and quotas; the AI only phrases questions and maps
  free-text replies to valid answers. It suits choice, number, and text questions; grids, rankings and
  file uploads aren't supported in chat. The browser chat at `/chat` is a preview that records
  nothing; to collect answers conversationally, an agent uses the MCP `begin_survey` /
  `answer_survey` tools, which hand it the structured result.
- **Contextual enrichment.** Look up a fact about the respondent at session start (from the fixture
  provider, the reference warehouse adapter, or a provider you write) and use it in logic, piping, or
  routing.
- **LLM analysis.** An opt-in `source: llm` enrichment derives sentiment, themes or a summary from
  open-text answers when the survey completes, and stores it on the response for reporting and
  export. It doesn't change the survey's flow and isn't shown to the respondent.

## Runs anywhere

- **Storage** (`SURVEY_STORE`): `file` for inspectable JSON/JSONL (the default, written to
  `./survey_data`), `sqlite` for a concurrency-safe single file, `memory` for tests, `mongo` as a
  reference multi-host backend, `http` to read and write through the API gateway, or your own class by
  import path (`my_pkg.store:PostgresStore`) implementing the store contract in
  [`store/__init__.py`](https://github.com/paulneto/survey-agent/blob/main/src/survey_agent/store/__init__.py).
  Picking a production database is the deployer's call, not the engine's.
- **Definition source** (`SURVEY_DEFINITION_SOURCE`): a parallel seam for the survey *definitions* and
  lifecycle. `file` (default) keeps them as `.survey.json` in git, so publishing or pausing is a
  redeploy. `mongo` (reference), `http`, or your own makes `survey-agent publish` mark a survey live
  **with no redeploy**, version-pinned.
- **Integrations** (`SURVEY_INTEGRATION`) decide what happens on completion or screen-out: `none`
  (the default; capture only), `redirect`, `webhook` (a POST to your API, Zapier, or n8n), a generic
  `panel` passback, `mailchimp`, or your own. Each delivers neutral lifecycle events with an
  idempotency key.
- **Persona sources** (`SURVEY_PERSONAS`) set where `pretest`'s synthetic respondents come from: an LLM
  or stub generating personas from the survey or a declared audience, or your own source
  (`your_pkg.personas:MyPersonaSource`). Format and plugin contract are in
  [docs/PERSONAS.md](https://github.com/paulneto/survey-agent/blob/main/docs/PERSONAS.md). This is for
  testing incidence and logic, not a substitute for real respondents.
- **Deploy** to a single container, a serverless function, or behind your own stack. See
  [docs/HOSTING.md](https://github.com/paulneto/survey-agent/blob/main/docs/HOSTING.md) for options
  and where to host the remote MCP connector. Themes (`dark`, `ocean`, `paper`, or your own) control
  look and feel.
- **Public runtime.** `survey-agent serve --public` binds `0.0.0.0` and serves only `live` surveys
  as a form, using whatever store and integration you configured (defaults: `file`, `none`).
  `serve --preview` serves drafts too, and only on localhost; it (and `demo`) also serves the `/chat`
  preview, which records nothing.

## Project layout

Importable code lives under `src/survey_agent/` (installed via `pip install -e .`); surveys, skills,
tests, and infrastructure stay at the repo root.

| Path | What |
|---|---|
| `src/survey_agent/engine/` | The pure-Python interpreter: schema, logic AST, piping, repeat, randomize, runtime, validate, compile_md, calc, export |
| `src/survey_agent/ai.py` · `conversation.py` · `consent.py` | Pluggable LLM client (Claude / OpenAI / offline stub), conversational runner, default consent and disclosure text |
| `src/survey_agent/cli.py` · `server.py` · `receiver.py` · `simulate.py` · `report.py` · `exporting.py` | The `survey-agent` command plus the runtime, mock receiver, simulator, and reporter / exporter |
| `src/survey_agent/{store,definitions,integrations,enrich,themes}/` | Storage seams, completion integrations, context providers, look and feel |
| `src/survey_agent/{mcp,api,sidecar}/` · `skills/` | MCP connector, API gateway, behavioral-trigger sidecar, Claude skills (at the repo root) |
| `surveys/` · `tests/` · `docs/` | Survey definitions, test suite, documentation (start at the [docs map](https://github.com/paulneto/survey-agent/blob/main/docs/README.md)) |
| `terraform/` · `Dockerfile` · `render.yaml` · `fly.toml` | Reference container and infrastructure configs for the hosted runtime |

## Configuration

Environment variables configure everything; `survey-agent init` also writes a
`survey-agent.config.yaml` that sets defaults for `store.backend`, `integration`, `theme`,
`output.dir`, and `deploy.command` (environment variables win). `survey-agent doctor` shows the
resolved configuration. See
[docs/ENGINEERING.md](https://github.com/paulneto/survey-agent/blob/main/docs/ENGINEERING.md#13-modules--extension-points)
for the plugin and extension points.

## Security and privacy

The engine never executes code from a survey: logic is a closed AST with no `eval`. Sending respondent
data to an LLM (conversational mode, `source: llm` analysis) is opt-in and disclosed; the authoring AI
sees definitions only. URL params that affect eligibility can be HMAC-signed. The API gateway
(`survey-agent api`) **runs without authentication unless you configure it**, and the operator console
has none of its own. Encryption at rest, retention, access control, audit logging and regulatory
compliance are the deployer's responsibility. Before you field to real people, run the
[operator checklist](https://github.com/paulneto/survey-agent/blob/main/docs/OPERATOR_RESPONSIBILITIES.md)
(store, erasure, consent, URL integrity, transport); it's printed at launch and, in a token-gated
deployment, acknowledged before a survey goes live. Full guidance:
[docs/DATA_SECURITY.md](https://github.com/paulneto/survey-agent/blob/main/docs/DATA_SECURITY.md).
To report a vulnerability, see
[SECURITY.md](https://github.com/paulneto/survey-agent/blob/main/SECURITY.md).

## Contributing

The canonical way to extend it (a new question type, store, context provider, integration, or theme)
is to describe it to an agent that scaffolds and tests it, with a machine gate
(`schema + lint + tests + simulation`) as the merge requirement. Start with
[CONTRIBUTING.md](https://github.com/paulneto/survey-agent/blob/main/CONTRIBUTING.md) for dev setup,
architecture rules, and how to add things. Please follow the
[Code of Conduct](https://github.com/paulneto/survey-agent/blob/main/CODE_OF_CONDUCT.md). Changes are
tracked in [CHANGELOG.md](https://github.com/paulneto/survey-agent/blob/main/CHANGELOG.md).

## License

Apache-2.0; see [LICENSE](https://github.com/paulneto/survey-agent/blob/main/LICENSE) and
[NOTICE](https://github.com/paulneto/survey-agent/blob/main/NOTICE). Free for anyone, including
commercial use. Apache-2.0 asks that you keep the license and attribution notices and note your
changes, and it includes an explicit patent grant.

CI runs the test suite and survey validation on every push (`.github/workflows/ci.yml`); releases
publish to PyPI via trusted publishing (`.github/workflows/publish.yml`).

---

*survey-agent began as an experiment in how survey and data-collection systems change when an agent
does the operating. Panel- and warehouse-specific adapters ship as separate plugins and aren't
required to run the engine.*
