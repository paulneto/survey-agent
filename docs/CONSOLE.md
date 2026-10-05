# survey-agent — operator help

The console (`survey-agent console`) is a **read-first GUI onto the engine** — the same actions the
agent runs, on a board. You monitor, operate, and start surveys from templates here; the **agent**
(Claude Code or another MCP-capable agent) does the authoring. (This file is what the console's
**Help** panel renders — edit it freely.)

> **Access:** the console binds **127.0.0.1** by default and is **unauthenticated by design**: anyone
> who can reach it can mark surveys live, pause, close, or delete them. If you expose it beyond your
> machine, put your own auth (a proxy, VPN, or trusted network) in front of it. See
> `SURVEY_CONSOLE_HOST` under [Configuration](#configuration-env--survey-agentconfigyaml).

## The survey lifecycle (the intended flow)

A survey moves through clear stages. Each stage names *who* does it — **console** (click), **agent**
(talk to it), or **CLI** (`survey-agent …`); they're the same engine underneath.

1. **Create — from a template** *(console or agent)*. Most surveys start from a template, not blank.
   In the console: **Templates** → open one → **Create draft** (enter an id + title). That scaffolds a
   **draft** in the store. Or tell the agent: *"draft a screener from the `screener` template about …"*.
2. **Refine / author** *(agent)*. The console scaffolds; it does **not** author. Open the draft with
   your agent — *"edit `<id>`: add a quota of 300, a screen-out for non-users, an upload step"* — the
   agent edits the definition. Authoring stays with the agent on purpose (logic is data, no clicky CMS).
3. **Check** *(console / agent / CLI)*. In the console, open the survey → **✓ Validate** (schema +
   consistency) and **🎲 Simulate** (the coverage gate — 200 synthetic respondents through every path;
   drop-off, screen-outs, est. length). A clean pass advances the lifecycle (`draft → validated →
   tested`). The **📊 Report** button shows the full coverage report. Deeper checks stay with the agent /
   CLI: `simulate -n 500`, `critique` (methodology), `pretest` (incidence).
4. **Approve** *(human)*. The **mandatory** sign-off before fielding real people. When
   `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface (CLI, console, or MCP connector) can mark a survey
   live without that token. When it is unset, the local CLI and console can mark live; the MCP
   connector still cannot.
5. **Publish + mark live** *(console or CLI)*. In the console: open the survey → **Mark live** (in a
   token-gated deployment, supply the approval token). With a runtime definition store
   (`SURVEY_DEFINITION_SOURCE=mongo` or `http`) that's a store write — **live, no redeploy**. With the `file` backend, `publish` / `git push` deploys it. The board's status light goes
   green. **`live` is the runtime's serving gate:** a production runtime (`serve --public`) serves
   respondents **only when a survey is live**
   (and by a separate **runtime** at `/s/<id>`, not the console). A pre-live survey shows respondents a
   *"not open yet"* page; only an operator can **preview** it. Marking live also **pins the launched
   version**. Running `survey-agent console` starts a local preview runtime (and a preview token) so
   **↗ As respondent** works immediately on any state; for real fielding, run `survey-agent serve --multi`
   (or point `SURVEY_PUBLIC_BASE_URL` at your hosted runtime).
6. **Operate** *(console)*. Watch the board (completes, incidence, status); **pause / resume / close**
   in one click (no redeploy on a runtime store); pull **responses** (CSV); open the all-surveys
   **Dashboard ↗** (params, quotas, live links, counts).
7. **Iterate** *(agent → publish)*. Edit, re-`publish` — the version auto-bumps; respondents mid-survey
   keep their pinned version.
8. **Retire** *(console)*. **Close** it (respondents see a closed page), or **Delete** it (detail →
   🗑 Delete, type the id to confirm). Delete removes the **definition + lifecycle**; the **collected
   responses are kept** in the response store for export/retention.

> **Create → test → live, in short:** Templates → Create draft (console) → refine with your agent →
> `validate` + `simulate` → approve → **Mark live** (console). All but the authoring and the deep
> coverage gate are one click in the console.

## Create a new survey from scratch

The console's **New survey** button only scaffolds **from a template**. To author a brand-new survey
from nothing, use the agent — that's what the console is a window onto:

- **In your agent (Claude Code / the connector):** run the **`/author-survey`** prompt with a
  plain-language brief — *"/author-survey screen US adults who stream paid video weekly; quota 300; screen
  out non-streamers"*. The agent drafts the spec, self-corrects on the coverage simulation, and writes
  `surveys/<slug>.survey.json` (+ a readable `.md`). It then appears on the board as a **draft**.
- **From a terminal (equivalent CLI):** `survey-agent author "<brief>"` does the same headless. (Needs a
  model key — `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`; `validate`/`simulate` run offline.)

Then refine with **`/edit-survey`** / **`/add-logic`**, check with **`/test-survey`** (or the board's
✓ Validate / 🎲 Simulate), get human approval, and **Mark live**. (Authoring stays with the agent on
purpose — logic is data, no clicky builder.)

## Creating a new template

Templates are **flat files**, discovered from the core package, installed plugins, and your overlay —
so a "new template" is a file you add (or ask the agent to scaffold). There's no template-builder UI by
design.

- **From an existing survey (easiest):** ask the agent — *"make a reusable template out of
  `surveys/<id>.survey.json`, parameterize the topic and audience"*. It writes the template files.
- **By hand:** add a folder `templates/<your-id>/` in your project (or under `overlay/templates/`, or
  a directory named by `SURVEY_TEMPLATES_DIR`) with:
  - `survey.json` — the survey spec, with `${name}` placeholders for the bits to fill in (e.g.
    `${slug}`, `${title}`, plus any params you declare).
  - `template.yaml` — a manifest: `id`, `title`, `description`, `tags`, and `params` (the placeholders
    + prompts).
  It appears in the console's **Templates** catalog (and `survey-agent templates`) automatically. See
  [templates/README.md](../templates/README.md) and [dev/TEMPLATES.md](dev/TEMPLATES.md) for the
  manifest fields. Templates specific to your organization belong in your own templates directory or a
  plugin, not the open core.

## In the console
- **Board** — every survey with a status light, completes, incidence, version, **grouped into
  collapsible project sections** (named groups A→Z, then *Ungrouped*; collapse state is remembered).
  Cached; re-pulls on the **↻ refresh** (shows "updating…") or after a state/project change. The filter
  box matches title, id, tags, **and project**. Click a row for detail.
- **Survey detail** — read-only preview (pages → questions → logic → terminals), the drop-in URL (copy),
  a **📁 Project** field to move it between groups (type a new name or pick an existing one — moving is a
  registry write, it never bumps the version), **✓ Validate** + **🎲 Simulate** (the launch checks, run
  live — results inline), a **live report**, **🧠 AI read-out** (the analyst insights — the one action
  that may call an LLM; falls back to a deterministic offline stub with no key; labelled with the engine
  + data source), **open as respondent** (uses the local/configured runtime; warns if none is up),
  **download responses**, **pause / resume / close / mark live**, and **🗑 Delete** (typed confirm).
- **Templates** — browse + preview; **Create draft** from one.
- **Tools** — New survey · **Dashboard ↗** (all-surveys management view) · Refresh · **Health** (the
  resolved config + a tally).
- **Help / Theme** — this page; a theme picker (Midnight, Light, Dracula, Nord, Gruvbox, Solarized,
  Tokyo Night, One Dark, Matrix, Amber).

## Lifecycle states
`draft` → `validated` → `tested` → `approved` → `live` → `paused` / `closed`. The runtime **enforces**
the lifecycle: it serves real respondents only when a survey is **`live`** (= deployed + collecting;
configurable via `SURVEY_SERVE_STATES`). `paused` / `closed` show their pages; the pre-live states
(`draft`/`validated`/`tested`/`approved`) show a *"not open yet"* page and are operator-previewable only
(preview collects no data). A survey with no state at all (a single-file `serve`) is unmanaged and served
as-is. The local `survey-agent demo` sandbox serves every state so you can click through drafts.

## Commands & options reference

Two surfaces drive the same engine; the console is a visual layer over them. **Agent prompts are slash
commands** (in Claude Code / the connector); **CLI commands are `survey-agent <command>`** (no slash). Run
`survey-agent <command> --help` for every flag, or `survey-agent help` for the guided flow.

### Agent prompts (slash commands)
- **`/author-survey`** — draft a brand-new survey from a plain-language brief (the from-scratch path).
- **`/edit-survey`** — change an existing survey (questions, pages, wording, terminals).
- **`/add-logic`** — add display logic / branching / quotas / screen-outs.
- **`/test-survey`** — validate + simulate (the coverage gate) and surface problems.
- **`/launch-survey`** — emit the drop-in `survey_url` and mark it live (human-approval gated).
- **`/survey-report`** — generate the report / analyst read-out.
- **`/inline-survey`** — run a survey inline in an agent conversation (`begin_survey` /
  `answer_survey`) to collect structured answers from a person mid-task.

### CLI commands (`survey-agent <command>`)
- **Author & shape:** `author "<brief>"` (draft from scratch — needs a model key) · `new <template> --slug <id>` (scaffold from a template) · `compile <file.md>` / `render <file.json>` (Markdown ⇄ JSON) · `schema` (print the JSON Schema).
- **Check:** `validate <file>` · `simulate <file> -n 500` · `critique <file>` · `pretest <file>`.
- **Preview:** `serve --multi` (or `serve --survey <file>`) · `convo --survey <file>` · `console` (this UI) · `demo` (clickable demo + webhook receiver).
- **Launch & publish:** `launch <file>` `[--base · --no-live · --deploy · --approve TOKEN · --ack-responsibilities]` · `publish <file>` (runtime store — live, no redeploy) · `sign-url <file> uid=… rid=…` (signed per-respondent link).
- **Lifecycle:** `status <id>` (show) · `status <id> --set live|paused|closed|approved [--url] [--note]` (set) · `status <id> --project "<name>"` (move to a project; `--project ""` ungroups) · `list [--json]` (all surveys + state).
- **Data subject requests:** `dsar <uid> [--survey ID] [--delete --yes]` (find, and optionally erase, a respondent's responses in the configured store).
- **Definitions (store ⇄ files):** `defs-export [--all]` · `defs-import` · `defs-diff`.
- **Report & data:** `report <file> [--simulate N --open]` · `insights <file>` · `export <file> [--format csv|parquet]` · `dashboard [--live --reports]`.
- **Project:** `init [dir]` (scaffold config + `.env` + example) · `templates` (list) · `doctor` (check install/config) · `help`.

> **Notation:** a leading `/` is an **agent prompt** (e.g. `/author-survey`); everything under
> `survey-agent …` is a **CLI command** (e.g. `survey-agent list` — no slash). There is **no** CLI `edit`
> command — editing a survey is the **`/edit-survey`** agent prompt.

## What to say to your agent (prompts)
- *"Draft a screener from the `data-download` template for a ChatGPT export study; screen out non-users."*
- *"Edit `chatgpt-p1`: add a quota of 500 and a file-upload step for the export zip."*
- *"Validate and simulate 500 through `chatgpt-p1`; show coverage and incidence; fix dead ends."*
- *"Make `chatgpt-p1` live"* / *"pause `tiktok-p1`"* / *"give me the drop-in URL for `chatgpt-p1`."*
- *"How is `chatgpt-p1` doing — completes, incidence, screen-outs?"* / *"cross-tab Q3 by role."*
- *"Turn `surveys/chatgpt-p1.survey.json` into a reusable template."*

## Configuration (env / survey-agent.config.yaml)
- `SURVEY_DEFINITION_SOURCE` — `file` (default) | `mongo` | `http` | import path. Where definitions +
  lifecycle live.
- `SURVEY_STORE` — `memory` | `file` (default, writes `./survey_data`) | `sqlite` | `mongo` | `http` |
  import path. Where responses/quotas live.
- `SURVEY_INTEGRATION` — `none` (default) | `redirect` | `webhook` | `panel` | `mailchimp` | import
  path. Terminal passback.
- `SURVEY_LAUNCH_APPROVAL_TOKEN` — when set, marking a survey live (console included) requires this
  token.
- `SURVEY_OUTPUT_DIR` — folder for generated reports/exports (default `reports/`).
- `SURVEY_PUBLIC_BASE_URL` — base for the drop-in survey URLs (optional on common hosts, which the
  runtime detects; `survey-agent doctor` shows the resolved value).
- `SURVEY_CONSOLE_HOST` — bind address for the console (default `127.0.0.1`; or `survey-agent console
  --host`). Use `0.0.0.0` to host it for a team — but the console is **unauthenticated** and exposes
  operator controls (mark live, delete, status), so keep it behind an auth proxy / VPN / trusted network;
  never expose it to the open internet. For a team deployment, run the console with
  `SURVEY_DEFINITION_SOURCE=http` / `SURVEY_STORE=http` so it reaches the data through the authenticated
  **API gateway** (`survey-agent api`, with auth configured) instead of a direct DB connection — see
  [HOSTING.md](HOSTING.md).
- `SURVEY_SERVE_STATES` — lifecycle states the runtime serves to respondents (default `live`). Widen
  during a migration (e.g. `live,approved,tested`), then tighten back.
- `SURVEY_PREVIEW_TOKEN` — token that lets an operator preview a pre-live survey (`?preview=<token>`).
  The console mints one when it auto-starts its local runtime; set the same value on both to preview
  against an external/shared runtime.
