# Quickstart: try survey-agent end to end

Kick the tires as a brand-new user: install, scaffold a survey, validate it, simulate every path,
then take it for real in your browser (with live webhook passbacks) and as an AI conversation. You
need no database or cloud: the demo uses Python's stdlib HTTP server and the default `file` store,
which writes responses under `./survey_data` in the folder you run it from.

Every command below is copy-pasteable and has been run start-to-finish.

## 1. Install

survey-agent is a normal Python package that installs a `survey-agent` command.

```bash
python -m venv .venv && source .venv/bin/activate
pip install survey-agent
```

> The PyPI package ships with the first release; until then install from GitHub:
> `pip install "git+https://github.com/paulneto/survey-agent"`
> (with extras: `pip install "survey-agent[llm] @ git+https://github.com/paulneto/survey-agent"`),
> or clone the repo and `pip install .`.

The base install pulls only `jsonschema` + `pyyaml`. Optional extras: `[llm]` (Anthropic-powered
authoring/analysis) or `[openai]` (GPT), `[api]` (the FastAPI control-plane gateway — `survey-agent
api`), `[mcp]` (the connector), `[mongo]` (the *reference* multi-host store — just `pymongo`), and
`[dev]` (tests). The `memory`, `file`, and `sqlite` stores need **no extra** (pure stdlib).

> **Which store?** Start with `file` (one box, one worker) → `sqlite` (one box, many workers,
> concurrency-safe, still one file, no server) → a database only when storage is shared across hosts
> or the disk is ephemeral. For a database, bring your own (`SURVEY_STORE=your.pkg:Store` — Postgres,
> SQLite-elsewhere, your warehouse) or use the reference `mongo` store. See the decision guide in
> [HOSTING.md](HOSTING.md#what-does-your-deployment-need-start-simple).

```bash
survey-agent help          # a guided tour of the commands + the author→launch→report flow
survey-agent doctor        # confirms the CLI runs THIS install + lists which extras/config are present
```

## 2. Scaffold a project

```bash
survey-agent init my-surveys && cd my-surveys
```

Pass a folder name (as above) to scaffold into it, or run `survey-agent init` with no argument to
scaffold into the folder you're already in. Either way it prints the absolute path it wrote to.

This writes `survey-agent.config.yaml`, a `.env` (all optional; the demo runs fully offline without
keys), and `surveys/example.survey.json`, a worked **streaming-services** screener (consent → usage
screen → service piping → behavioral "why" → quotas → redirect) that also demonstrates a **video
stimulus** and a **webhook action**. To enable natural conversational phrasing or LLM text analysis,
put `ANTHROPIC_API_KEY=sk-ant-...` in `.env` (auto-loaded); otherwise an offline stub is used.

> **Identity is configurable.** The example uses `uid` (respondent) + `rid` (response). To match a
> system that sends other names (say `pid` and `panel_id`), add
> `settings.identity: {respondent: pid, response: panel_id}`, and the session key, dedup, enrichment
> lookup, and erasure all follow it.

## 3. Validate

```bash
survey-agent validate surveys/example.survey.json
```

Runs JSON-Schema + a semantic lint (dead branches, dangling refs, missing terminal, undeclared
URL variables, merge tokens that point nowhere, and display logic that depends on a question on the
same or a later page). This is the publish gate.

## 4. Simulate every path

```bash
survey-agent simulate surveys/example.survey.json -n 500
```

500 synthetic respondents traverse every branch. You'll see screen-out reasons, the **passbacks that
would fire** (completed/terminated), quota counters, and an estimated length of interview, ending with:

```
✅ coverage gate passed: complete + every declared screen-out reachable; all callbacks mapped.
```

## 5. Take it in your browser (the real drop-in flow)

```bash
survey-agent demo
```

This starts two things and opens your browser:

- the **survey runtime** on <http://127.0.0.1:8000>
- a **mock webhook receiver** on <http://127.0.0.1:9000> that prints every event. It stands in for
  your panel / CRM / API.

The demo serves surveys in every lifecycle state, so drafts are takeable without marking them live.

Open <http://127.0.0.1:8000> and pick the survey, or go straight to a survey URL with the drop-in
parameters:

```
http://127.0.0.1:8000/s/streaming-usage?uid=cur1&rid=1&group=test
```

`uid`, `rid`, and `group` are captured into `url_variables` exactly as in production. As you click
through:

- the intro page plays a **video** (a YouTube link rendered in privacy mode, no tracking cookies);
- answering **"Currently subscribe"** fires the **webhook action**. Watch the receiver terminal print it:

  ```
  [RECEIVER] uid=cur1 rid=1 STARTED          event=started
  [RECEIVER] ...                QUALIFIED_LEAD  event=qualified_lead
  ```

- finishing (or screening out) fires the matching lifecycle **passback** to the receiver.

That's the whole contract end to end: `survey_url` → server-driven pages + logic → terminal →
passback, plus a conditional mid-survey webhook.

> Handy flags: `survey-agent demo --quota 2` (a tiny quota, so you hit over-quota fast),
> `--no-browser`, and `--port` / `--port-receiver`. The example's webhook action posts to the default
> receiver on `:9000`.

## 6. Take the same survey as an AI conversation

```bash
survey-agent convo
```

The identical survey JSON is administered as a chat. The **engine still owns** flow, branching,
screen-out, and quotas; the LLM only phrases each question and maps your free-text reply to a valid
answer. On a thin open-text answer it may ask **one adaptive follow-up** (opt-in via `probe: true` /
`settings.ai`; stored as a derived field, never changes the flow). Offline it uses a deterministic,
fairly mechanical stub; set `ANTHROPIC_API_KEY` (and install `[llm]`) for natural phrasing.

Non-interactive, pass the answers in order with `--scripted`. With the offline stub this walks the
bundled example to a completion:

```bash
survey-agent convo --scripted yes 34 "Currently subscribe" Netflix "Checking whether the price went up" Netflix "Strongly agree"
# ...
# [survey terminal: completed]

survey-agent convo --scripted no
# [survey terminal: disqualified]
```

(With a real LLM key the AI may add a follow-up probe turn, so a fixed script is for the offline
stub.) The same chat runs in the browser at `/chat` under `survey-agent serve`/`demo`.

Conversation mode suits single-select, multi-select, number, and text questions. **Grids, rankings,
and file uploads aren't supported in conversation mode**, and it doesn't fall back to a form, so
deliver surveys that use them as a form.

## 7. See the results: report + export

```bash
survey-agent report surveys/example.survey.json --simulate 500 --open
survey-agent report surveys/example.survey.json --simulate 500 --insights --open
survey-agent insights surveys/example.survey.json --simulate 500
survey-agent export surveys/example.survey.json --simulate 500
```

`report` writes a single HTML file: outcome funnel + incidence, length-of-interview, a chart per
question (bar/histogram), NPS, derived/segment fields, and quota burndown (charts via Chart.js; the
data is embedded in the file). Add **`--insights`** for an AI analyst section at the top, with a
headline, key findings, open-end themes, watch-outs, and suggested follow-ups (or `survey-agent
insights` to print it). `export` writes a flat grid (`id`, `status`, `url.*`, every question, with
one column per item for a repeated question, `derived.*`) as CSV; add `--format parquet` with the `[data]` extra. Drop `--simulate N` to report on
/ export the **real** responses in your store instead of synthetic ones. (These CLI tools read your
*local* `SURVEY_STORE`; to analyze a **hosted** Mongo set `SURVEY_STORE=mongo` + `SURVEY_MONGODB_URI`
first, or just pull over the token-gated API — see [DEPLOY.md](DEPLOY.md). `survey-agent dashboard
--live` shows live completes/quota for every survey at once.)

## What next

- **Write your own survey.** Drop a new `surveys/<slug>.survey.json`, start from a template
  (`survey-agent templates`, then `survey-agent new <template> --slug <id>`), or author the friendlier
  Markdown form and `survey-agent compile surveys/<slug>.md`. Full capability list:
  [FEATURE_COVERAGE.md](FEATURE_COVERAGE.md).
- **See question and page repeating.** In a clone of the repo,
  `surveys/streaming-brand-ratings.survey.json` rates each service the respondent picked, follows up
  only on the ones rated 4 or 5, and repeats a page once per household member. It isn't live, so
  preview it with a token (preview sessions collect nothing):
  `SURVEY_PREVIEW_TOKEN=try survey-agent serve --survey surveys/streaming-brand-ratings.survey.json --port 8077`,
  then open `http://127.0.0.1:8077/s/streaming-brand-ratings?uid=1&rid=1&preview=try`.
- **Deploy a hosted runtime (one service, many surveys).** Run the tiny stdlib runtime for real
  fielding — `survey-agent serve --public --multi` (serves only `live` surveys), with a file,
  `sqlite`, or `[mongo]` store, and Render blueprints (paid plan) among other hosting options:
  [DEPLOY.md](DEPLOY.md) and [HOSTING.md](HOSTING.md). (For a team deployment, front the store with
  the **API gateway** (`survey-agent api`, the `[api]` extra); it runs open unless you configure auth:
  [ENGINEERING.md](ENGINEERING.md#12-what-runs-where).)
- **Agentic authoring.** The seven skills in `skills/` (`author-survey`, `edit-survey`, `add-logic`,
  `test-survey`, `launch-survey`, `survey-report`, `inline-survey`) and the MCP connector (`[mcp]`)
  drive brief → draft → validate → simulate → human-approve → launch: [USER_GUIDE.md](USER_GUIDE.md),
  [SKILL_WALKTHROUGH.md](SKILL_WALKTHROUGH.md), and [AGENTIC_MODES.md](AGENTIC_MODES.md).
- **Security & privacy** (LLM boundaries, signed params, webhooks, erasure):
  [DATA_SECURITY.md](DATA_SECURITY.md).
