# Deploy walkthrough: create a survey → deploy → pull the data

A hands-on, end-to-end guide for running survey-agent as a small **team service**: one deployment serves
many surveys, your team authors them in git, and you pull results over an API. One example is carried
start to finish.

> **This walkthrough uses Render (and, optionally, the reference Mongo store) as one concrete worked
> example — not a requirement or a recommended default.** survey-agent runs on any
> container/serverless/PaaS host or your own servers, and the store is a ladder (`file` → `sqlite` → a
> database, your own or the reference `mongo`). To choose what *your* deployment needs, read the
> decision guide in [HOSTING.md](HOSTING.md#what-does-your-deployment-need-start-simple), then come
> back here for the mechanics. Everything below works with the host and store you pick — swap the
> names accordingly.

> ### Replace these placeholders first (search-and-replace this doc for your own values)
> | Placeholder | Replace with |
> |---|---|
> | `<your-org>` | your GitHub org or username (where your **private deploy repo** lives) |
> | `<engine-repo>` | the survey-agent engine repo, e.g. `github.com/paulneto/survey-agent` (or your fork) |
> | `my-surveys` | your service / deploy-repo name — used as the running example here; rename freely |
> | `~/survey-agent` | your local clone of the survey-agent **engine** |
> | `~/my-surveys` | your local clone of the **deploy repo** |
>
> In the Render example your live URL will be `https://my-surveys.onrender.com`; on another host,
> substitute its URL.

**The two repos.** Keep them separate:
- **`<engine-repo>` (survey-agent)** — the engine. Kept clean; not connected to your host.
- **`<your-org>/my-surveys` (private)** — the team repo you clone and work from: engine + your surveys +
  your host config (e.g. `render.yaml`). Your host deploys this. Pull engine updates down via `upstream`.

> The example: a short **"AI at Work"** screener — who uses AI assistants for work, which ones, what
> for, and how much time they save — with a screen-out and a role quota. Slug: `ai-at-work`.

---

## First-time setup (each teammate, once)

Someone has added you to the `<your-org>/my-surveys` repo and — if you'll pull data — given you the
`SURVEY_API_TOKEN`. Then, once on your machine:

```bash
git clone https://github.com/<your-org>/my-surveys.git ~/my-surveys   # or wherever you keep code
cd ~/my-surveys
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[llm,mongo,dev]"   # drop `mongo` if your deployment doesn't use it
cp .env.example .env        # add your own ANTHROPIC_API_KEY (or OPENAI_API_KEY) for local authoring
survey-agent doctor         # confirms the CLI runs THIS repo's code + shows extras/config
```

> ### Running more than one clone? Check `doctor` first.
> A Python env allows only **one** editable install per package. If you `pip install -e .` in one clone
> and later clone the repo again elsewhere, `survey-agent` keeps running the **first** clone's code — so
> you'd silently get stale features. `survey-agent doctor` flags this (and the CLI prints a stderr
> warning automatically). The one-liner behind it:
> ```bash
> python -c "import survey_agent, os; print(os.path.dirname(survey_agent.__file__))"
> # → should be a path inside THIS folder. If it points at another clone, run `pip install -e .` here.
> ```

> **Prefer a desktop agent app to a terminal?** If the app's agent has a shell (a "code" surface), the
> commands below just work. A no-terminal surface drives survey-agent through its MCP **tools**
> instead — one-time connector setup in **[Appendix D](#appendix-d--authoring-from-claude-desktop-cowork--code)**.

You do **not** create the repo, seed it, or deploy — that's a one-time setup job
([Appendix A](#appendix-a--one-time-setup-the-service)). You just clone + install, then build surveys.

## Then, for each survey, start here

```bash
cd ~/my-surveys && source .venv/bin/activate
git pull
git checkout -b survey/ai-at-work        # one branch per survey
```

---

## Step 1 — Create the survey (author from a brief)

In an agent that loads the repo's skills (`skills/`), describe what you want:

```
/author-survey  "AI at Work" screener, ~2 minutes. Ask:
  1) Do you use AI assistants for work?  (weekly / occasionally / no)
  2) Which ones?  (multi-select)
  3) What do you mainly use them for?  (open text)
  4) Roughly how much time do they save you per week?  (<1h / 1–3h / 3–5h / 5h+)
  5) Your role?  (IC / manager / exec / other)
Open with a one-line intro + consent.
```

(No agent? `survey-agent author "<brief>"` does the same from the CLI with a model key configured.)

It writes two files:

```
surveys/ai-at-work.survey.json     # the spec the engine runs
surveys/ai-at-work.md              # the same survey, human-readable
```

The JSON is plain and reviewable — each question is data, e.g.:

```jsonc
{ "id": "uses_ai", "type": "radio", "required": true,
  "title": "Do you use AI assistants for work?",
  "options": [ {"value":"weekly","label":"Yes — most weeks"},
               {"value":"sometimes","label":"Occasionally"},
               {"value":"no","label":"No"} ] }
```

Read `surveys/ai-at-work.md` to sanity-check the wording. To regenerate one from the other after a
hand-edit: `survey-agent render surveys/ai-at-work.survey.json --write` (JSON → Markdown) or
`survey-agent compile surveys/ai-at-work.md --write` (Markdown → JSON).

---

## Step 2 — Shape it: edits + logic

**Reword / tweak:**
```
/edit-survey surveys/ai-at-work.survey.json  — make Q3 optional, and add another tool to the list
```

**Add the real logic** (skip/branch, screen-outs, piping and quotas are stored as *data* — an AST, no code):
```
/add-logic surveys/ai-at-work.survey.json:
  • screen out anyone who answers "No" to Q1  → terminate as disqualified
  • only show the "which tools" and "time saved" questions to AI users (later page)
  • pipe their top tool into Q3's wording
  • for each tool they picked, ask how often they use it (a page that repeats once per tool)
  • quota: cap 300 completes, max 100 per role  → over-quota terminal
```

A screen-out ends up as a terminal rule the engine evaluates, e.g.:

```jsonc
{ "type": "disqualify", "reason_code": "non_user",
  "when": { "ref": {"kind": "question", "id": "uses_ai"}, "operator": "equals", "value": "no" },
  "message": "Thanks! This one's just for people who use AI tools at work." }
```

> Rule of thumb the engine enforces: a question that *depends on* another answer goes on a **later
> page** (server-driven flow). The skill handles that for you, and `validate` rejects it if not.

---

## Step 3 — Check it before anyone sees it

```bash
survey-agent validate surveys/ai-at-work.survey.json
#   → schema + semantic lint: every option/branch/quota/merge token resolves, and no question is
#     gated on an answer from its own or a later page.

survey-agent critique surveys/ai-at-work.survey.json
#   → methodology review: flags leading or double-barreled questions, missing "prefer not to say",
#     bad numeric ranges. Fix what you agree with.

survey-agent simulate surveys/ai-at-work.survey.json --n 500
#   → THE COVERAGE GATE. Runs 500 synthetic respondents through the survey and reports incidence,
#     drop-out, estimated length-of-interview, and any page/branch/quota never exercised. On success:
#     "✅ coverage gate passed: complete + every declared screen-out reachable; all callbacks mapped."

survey-agent pretest surveys/ai-at-work.survey.json --n 6
#   → persona pre-test: synthetic personas answer the survey for a realistic incidence preview.
#     With a real model key each persona makes ~one request per question, so keep N small.
```

Deeper, conversationally: `/test-survey surveys/ai-at-work.survey.json` walks the logic, routing,
quotas, and passbacks and tells you what it found.

---

## Step 4 — Preview it exactly as a respondent will

The production runtime only serves `live` surveys, so preview drafts locally with `--preview`:

```bash
survey-agent serve --preview --multi --dir surveys --port 8000
```
Open both surfaces and actually take it:
- **Form:** http://localhost:8000/s/ai-at-work
- **Conversation:** http://localhost:8000/chat?survey=ai-at-work  (same survey, as a chat)

Try the paths on purpose: answer **"No"** to Q1 → you should hit the **disqualified** screen; answer
**"weekly"** → you should see the tools + time-saved pages with your tool piped in. `Ctrl-C` to stop.
(Responses from local testing land in `./survey_data/` and are disposable. `survey-agent demo` is
another local sandbox: it serves every lifecycle state plus a mock passback receiver on :9000.)

---

## Step 5 — Launch + deploy it (live, shareable URL)

Launching fields to real people, so get sign-off first. The lifecycle is
`draft → validated → tested → approved → live`, with `paused` and `closed` to stop fielding.

**1. Mark it live and get the URL.**

```bash
survey-agent launch surveys/ai-at-work.survey.json --base https://my-surveys.onrender.com
```

This prints the drop-in `survey_url` and records `live` in `surveys/.registry.json`. Marking live is
local bookkeeping — it does **not** publish. The approval gate:

- If your deployment sets `SURVEY_LAUNCH_APPROVAL_TOKEN`, nothing (CLI `launch` / `status --set live` /
  `publish`, the console, or MCP) marks a survey live without the token: a human runs
  `survey-agent launch … --approve <token> --ack-responsibilities` (or sets `SURVEY_OPERATOR_ACK=1`
  for standing sign-off of the [operator checklist](OPERATOR_RESPONSIBILITIES.md)).
- If it is unset, the local CLI and console can mark live directly (the MCP connector still can't), so
  make your PR review the approval step.

**2. Publish it.** With file-based definitions (the default), publishing is a deploy:

```bash
git add surveys/ai-at-work.survey.json surveys/ai-at-work.md surveys/.registry.json
git commit -m "Add AI-at-Work screener"
git push -u origin survey/ai-at-work
gh pr create --fill          # a teammate reviews → merges to main
```

Merge to `main` → **the host redeploys** → your survey is **live** (make auto-deploy reliable with the
deploy-hook setup in [Appendix A](#appendix-a--one-time-setup-the-service)):

```
https://my-surveys.onrender.com/s/ai-at-work
```

That's the drop-in link — put it in your panel, email, or CRM. Every query parameter you append
(`?uid=…&rid=…&group=…`, or whatever names your panel sends) is captured into `url_variables` so you
can tie responses back to people. If a parameter affects eligibility, quota, routing, or payment, mint
**signed** per-respondent links instead:

```bash
export SURVEY_URL_SECRET=…            # the same secret the runtime has
survey-agent sign-url surveys/ai-at-work.survey.json uid=123 rid=456 group=newsletter \
  --base https://my-surveys.onrender.com
```

…and set `settings.url_signing.required` in the survey so the runtime rejects unsigned or edited links.
Adding *another* survey later is just another file + push — no new service.

> **Without the redeploy — a runtime definition store.** The above is the default (`file`) model:
> publishing = a git push → redeploy. If you set `SURVEY_DEFINITION_SOURCE=mongo` (the reference
> backend, reusing `SURVEY_MONGODB_URI`/`SURVEY_MONGODB_DB`), `http` (via the API gateway), or your own
> import path, publishing is a **store write instead**:
> ```bash
> survey-agent publish surveys/ai-at-work.survey.json   # validate → store → live, NO redeploy
> ```
> `publish` is subject to the same approval gate. The survey is live immediately and version-pinned
> (in-flight respondents keep their version); commit the file too as the readable backup. Pause/close
> the same way with no redeploy (`survey-agent status ai-at-work --set paused`), and use
> `defs-export --all` / `defs-import` / `defs-diff` for cold backup, store-loss rebuild, and file↔store
> reconciliation. See [Appendix C](#appendix-c--command-cheat-sheet) → "Where definitions live".

---

## Step 6 — Collect + pull the data

As people respond, the service stores each response — as a flat file
(`/var/data/responses/ai-at-work.jsonl`) with the `file` store, or in the database you configured.
Either way the simplest way to pull them is the runtime's **token-gated API**: it reads whatever store
the service is on, so the commands below are identical for any store.

```bash
export TOKEN=<SURVEY_API_TOKEN — from your host's environment settings>

# JSON
curl -H "Authorization: Bearer $TOKEN" https://my-surveys.onrender.com/api/responses/ai-at-work
```
```json
{ "result_ok": true, "survey_id": "ai-at-work", "count": 2,
  "data": [
    { "id": "3f9c2a7e5b1d4c08a6e2f71b9d0c4e55", "survey_id": "ai-at-work", "status": "Complete",
      "date_started": "2026-06-10 18:48:32", "date_submitted": "2026-06-10 18:51:04",
      "url_variables": { "uid": "7", "rid": "42", "group": "newsletter" },
      "survey_data": {
        "uses_ai":   { "id": "uses_ai", "question": "Do you use AI assistants for work?", "answer": "Yes — most weeks" },
        "tools":     { "id": "tools", "question": "Which ones?", "answer": ["Tool A", "Tool B"] },
        "time_saved":{ "id": "time_saved", "question": "Time saved per week?", "answer": "3–5h" },
        "role":      { "id": "role", "question": "Your role?", "answer": "IC" } },
      "derived": {} } ] }
```

The `id` is an opaque, system-generated response id (also the passback idempotency key); identity
lives in `url_variables`. `status` is `Complete` for a completion, `Disqualified` for a screen-out or
over-quota (with the rule's `reason_code`), and `Partial` for an unfinished session. Answers are
display labels.

```bash
# CSV (one row per respondent; answers flattened to q.<id> columns)
curl -H "Authorization: Bearer $TOKEN" \
  "https://my-surveys.onrender.com/api/responses/ai-at-work?format=csv" -o ai-at-work.csv

# everything across all surveys
curl -H "Authorization: Bearer $TOKEN" https://my-surveys.onrender.com/api/responses
```

A repeated question (asked once per tool, say) has one `survey_data` entry per item, keyed
`qid[item]` (`freq[chatgpt]`) with an extra `repeat: {source, value, label}`, and one CSV column per
item in the source question's option order.

The `?token=` query form works too if a header is awkward. **With `SURVEY_API_TOKEN` unset the
endpoint is disabled (503); a missing or wrong token gets 401.** Exports are not redacted — treat the
token like database credentials.

### Pulling from the store directly (Mongo example)

The API above is the recommended path — no database credentials on your laptop, and it's the same call
whatever the backend. If you use the reference Mongo store and want to query it directly (ad-hoc
analysis, or to run the CLI report/export tools against production), point a client at the **same
database** the service uses.

**With the survey-agent CLI** — `export`/`report`/`insights` read your *local* `SURVEY_STORE`, so aim
it at the production store first (otherwise you're reading your local `./survey_data`, not production):
```bash
export SURVEY_STORE=mongo
export SURVEY_MONGODB_URI="mongodb+srv://…"   # the same string your service uses
export SURVEY_MONGODB_DB=survey_agent
survey-agent export surveys/ai-at-work.survey.json            # → CSV (--format parquet for Parquet)
survey-agent report surveys/ai-at-work.survey.json --open     # → HTML report from live data
```

**With `mongosh` or a GUI client** — raw documents. The store uses three collections: `responses` (the
export records — this is what you want), `sessions`, and `quota_counters`. Each response doc is the
export shape above: `{ survey_id, id, status, date_started, date_submitted, url_variables, survey_data, derived, … }`:
```bash
# count responses for one survey
mongosh "$SURVEY_MONGODB_URI" --eval \
  'db.getSiblingDB("survey_agent").responses.countDocuments({survey_id:"ai-at-work"})'

# the completed responses themselves
mongosh "$SURVEY_MONGODB_URI" --eval \
  'db.getSiblingDB("survey_agent").responses.find({survey_id:"ai-at-work", status:"Complete"}).toArray()'
```
(`mongosh` is MongoDB's shell, a separate install. `pip install ".[mongo]"` if you want `pymongo`
locally for your own scripts.)

---

## Step 7 — Report & analyze

```bash
survey-agent report  surveys/ai-at-work.survey.json
#   → completes, incidence, median LOI, screen-out/over-quota counts, and question-level cross-tabs.

survey-agent insights surveys/ai-at-work.survey.json
#   → an AI analyst read-out: headline, key findings, themes from the open text, suggested next steps.
#     With a model key this sends aggregate stats + a sample of open-text answers to your provider.

survey-agent export  surveys/ai-at-work.survey.json   # raw CSV (or --format parquet) for your own BI tooling
```
> These read your **local** `SURVEY_STORE`. Point it at the production store first — see
> [Pulling from the store directly](#pulling-from-the-store-directly-mongo-example) in Step 6 — or
> you'll be reporting on your local store instead of production.

Or just ask your agent: **"pull the ai-at-work results and summarize the open-text themes by role."**
`/survey-report ai-at-work` does the same conversationally.

---

## Step 8 — Iterate (definitions are versioned)

Change wording, add a question, tighten a quota — same loop:
```bash
/edit-survey surveys/ai-at-work.survey.json …      # or /add-logic …
survey-agent validate surveys/ai-at-work.survey.json && survey-agent simulate surveys/ai-at-work.survey.json --n 500
git commit -am "ai-at-work: add follow-up on budget" && git push    # PR → merge → redeploy
```
Definitions are **immutable + versioned**: respondents already mid-survey stay on the version they
started; new respondents get the new one.

---

## See what's live (surveys + URLs)

Every survey is a file in git, deployed at `/s/<id>` — but **the runtime only serves a survey to real
respondents once it's `live`** (the lifecycle gate; pre-live surveys show a "not open yet" page). So
the repo is the roster of what's *deployed*; `state == live` is the roster of what's *open*. Mark live
with `survey-agent launch` (Step 5), `survey-agent status <id> --set live`, or the console. (During a
first rollout you can set `SURVEY_SERVE_STATES=live,approved,tested` on the runtime so nothing shows
"not open yet" before you've marked each survey live, then tighten it back to `live`.) From your
`~/my-surveys` clone:

```bash
survey-agent list                 # every survey + lifecycle state
survey-agent list --json          # the same, machine-readable (id, state, tags, recorded live URL, …)
survey-agent status ai-at-work    # one survey's full record (state, note, recorded live URL)
```

```text
4 survey(s) in surveys/
  🟢 product-feedback      live      1.3m · url↗ · #feedback,screener
  ✅ social-platform-use   tested    1.2m
  …
```

Every survey is reachable at `<base>/s/<id>`, so to print the drop-in URL for each:

```bash
export BASE=https://my-surveys.onrender.com
survey-agent list --json | python3 -c '
import json, sys, os
base = os.environ["BASE"]
for e in json.load(sys.stdin):
    print("%-24s %-10s %s/s/%s" % (e["id"], e["state"], base, e["id"]))'
#   product-feedback     live     https://my-surveys.onrender.com/s/product-feedback
#   social-platform-use  tested   https://my-surveys.onrender.com/s/social-platform-use   …
```

Record where a survey is fielding (the `/launch-survey` skill does this for you; same approval gate):

```bash
survey-agent status ai-at-work --set live --url https://my-surveys.onrender.com/s/ai-at-work
```

**On the running service itself:**
- **`https://my-surveys.onrender.com/`** — a neutral holding page by default (it doesn't enumerate
  your surveys to anyone who visits). Set `SURVEY_LANDING_LIST=1` on a trusted deployment to list the
  surveys instead. `GET /health` returns `{"ok": true}` for uptime checks.
- **`survey-agent dashboard --live`** — writes a self-contained `dashboard.html` that opens locally:
  every survey with its **drop-in URL**, **accepted params**, and **live counts** (completed · incidence ·
  quota fill) pulled from the store. Point your shell at the production store first (Step 6) and pass
  the public base so the URLs are real:
  ```bash
  export SURVEY_STORE=mongo SURVEY_MONGODB_URI="mongodb+srv://…" SURVEY_MONGODB_DB=survey_agent
  survey-agent dashboard --live --base-url https://my-surveys.onrender.com --open
  ```
  Add `--reports` to also write each survey's full HTML report and link it, or `--simulate N` for
  synthetic numbers instead of live ones. (There's also an in-service console at `/manage`, but it's
  opt-in `SURVEY_CONSOLE=1` and **unauthenticated** — don't enable it on a public service unless it sits
  behind your own auth proxy.)

> **Heads-up — the runtime enforces the lifecycle.** It serves real respondents **only when a survey is
> `live`** (configurable via `SURVEY_SERVE_STATES`, default `live`). The pre-live states
> (`draft`/`validated`/`tested`/`approved`) show a **"not open yet"** page — operators preview them on a
> hosted runtime with `SURVEY_PREVIEW_TOKEN` (`/s/<id>?preview=<token>`; preview collects no data), or
> locally with `serve --preview`. **`paused`/`closed`** show their pages and start **no new sessions**
> (in-flight respondents may finish). Marking live pins the launched version. So:
> ```bash
> survey-agent status ai-at-work --set paused    # stop fielding (respondents see a "paused" page)
> survey-agent status ai-at-work --set live      # resume
> survey-agent status ai-at-work --set closed    # stop fielding for good ("closed" page)
> ```
> With file-based definitions, commit the `surveys/.registry.json` change and deploy for it to take
> effect on the hosted runtime. To remove a survey entirely, delete its file and redeploy.

---

# Appendix A — One-time setup (the service)

Done once to stand up `<your-org>/my-surveys` and deploy it. This uses Render as the example host; on
any other host, the runtime is the same command (`survey-agent serve --public --multi`, which is also
the [`Dockerfile`](../Dockerfile)'s default) — see [HOSTING.md](HOSTING.md).

```bash
# 1 — create the private team repo
gh repo create <your-org>/my-surveys --private -d "my-surveys — multi-survey runtime"

# 2 — seed it with the engine code (run this push from your survey-agent clone)
git -C ~/survey-agent push https://github.com/<your-org>/my-surveys.git main:main

# 3 — clone it to its working home
git clone https://github.com/<your-org>/my-surveys.git ~/my-surveys
cd ~/my-surveys
git remote add upstream https://<engine-repo>.git   # to pull engine updates later

# 4 — install + verify
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[llm,mongo,dev]" && pytest -q
survey-agent doctor         # confirms the CLI runs THIS clone's code (catches the multi-clone trap)
cp .env.example .env        # add ANTHROPIC_API_KEY (or OPENAI_API_KEY) for real-model authoring

# 5 — deploy on Render: New + → Blueprint → connect <your-org>/my-surveys → Apply
#     render.yaml provisions the service + a /var/data disk + an auto-generated SURVEY_API_TOKEN.
#     The runtime derives its public URL from RENDER_EXTERNAL_URL — no SURVEY_PUBLIC_BASE_URL needed
#     (set it only to override, e.g. for a custom domain). Copy SURVEY_API_TOKEN for the team.
```
The shipped `render.yaml` requests Render's paid **Starter** plan (a persistent disk requires a paid
instance) plus a 1 GB disk; check Render's current pricing. Then give the team repo access, share the
token, set any secrets you use (`SURVEY_URL_SECRET`, `SURVEY_WEBHOOK_SECRET`,
`SURVEY_PANEL_SHARED_SECRET`, `SURVEY_LAUNCH_APPROVAL_TOKEN`), and protect `main` (so a merge = a
reviewed deploy).

> ### Continuously fielding? Pick the zero-downtime blueprint.
> The default `render.yaml` keeps responses in flat files on a persistent disk. A Render disk mounts to
> **one instance at a time**, so Render can't run old+new instances during a deploy — it stop-starts the
> single instance, a short outage across every survey. If you field in bursts that's fine. If
> respondents arrive **continuously**, every survey launch (= a deploy) would interrupt in-progress
> respondents on all your other surveys.
>
> One fix ships in the repo: **`render.mongo.yaml`** — diskless, with a MongoDB database as the store
> (see [HOSTING.md → Storage](HOSTING.md#storage-pick-the-simplest-rung-that-fits); any store that isn't
> disk-bound works, including your own). With no disk, Render does a **rolling, zero-downtime** deploy
> (bring up the new instance, health-check, then switch). It needs a MongoDB you can reach from Render —
> self-hosted or a managed service:
>
> **a. Set up the database (once):**
> 1. Create a MongoDB database (any managed MongoDB service or your own server).
> 2. Create a database user with read/write access to it.
> 3. Allow connections from your host's egress. PaaS hosts often have dynamic outbound IPs; if your
>    provider can't allowlist them you may need a wide access list — rely on strong credentials and TLS.
> 4. Copy the connection string (e.g. `mongodb+srv://<user>:<password>@<cluster>/`).
>
> **b. Point the deploy repo at the diskless blueprint:**
> ```bash
> cp render.mongo.yaml render.yaml          # Render's Blueprint reads render.yaml at the repo root
> git commit -am "Use diskless + Mongo blueprint (zero-downtime)" && git push
> ```
> **c. Apply the Blueprint** as in step 5, and in the Render dashboard paste your connection string into
> `SURVEY_MONGODB_URI`. Nothing else changes — surveys stay files in git, and the approval gate and
> `merge = a reviewed deploy` still hold; only the *store* moved off the disk. (Launching a new survey
> is still a redeploy, but a zero-downtime one. Responses already on the old `/var/data` disk don't
> migrate automatically — pull them via `/api/responses`, or `survey-agent backup` / `restore`, first.)

> ### Make auto-deploy reliable (so `git push` actually publishes)
> Merging to `main` is supposed to trigger a redeploy, but a host's native auto-deploy webhook can
> silently fail — a survey then 404s on the live service because the push never deployed. A robust fix
> is to trigger the deploy **from GitHub Actions** via a **deploy hook** (on Render: a stable,
> per-service URL that starts a deploy when called — no API key).
>
> **One-time setup (Render example):**
> 1. **Create the hook:** Render → your service → **Settings → Deploy → Deploy Hooks → Create** → copy
>    the URL (`https://api.render.com/deploy/srv-…?key=…`).
> 2. **Add it to GitHub:** your deploy repo → **Settings → Secrets and variables → Actions → New
>    repository secret** → name `RENDER_DEPLOY_HOOK`, value = that URL.
> 3. **Add the workflow** to your deploy repo at `.github/workflows/deploy.yml`:
>    ```yaml
>    name: Deploy to Render
>    on:
>      push:
>        branches: [main]
>    jobs:
>      deploy:
>        runs-on: ubuntu-latest
>        steps:
>          - name: Trigger Render deploy hook
>            run: curl -fsS "${{ secrets.RENDER_DEPLOY_HOOK }}"
>    ```
> After that, every push to `main` deploys reliably. (Turn off the host's own auto-deploy to avoid
> double-deploys, or leave it as a backup.)
>
> **Manual / local one-shot:** [`deploy.sh`](../deploy.sh) ships in the repo — `./deploy.sh "msg"` does
> commit + push, and if `RENDER_DEPLOY_HOOK` is set (e.g. in `.env`) it also pings the hook directly.
> Any deploy command can also be wired to `survey-agent launch --deploy`:
> ```yaml
> # survey-agent.config.yaml
> deploy: { command: 'curl -fsS "$RENDER_DEPLOY_HOOK"' }
> ```
> (The engine repo deliberately doesn't ship a live `.github/workflows/deploy.yml` — it would run in
> the engine repo too; copy the snippet above into *your* deploy repo.)

> Hosting isn't Render-specific — the runtime is a stdlib server that reads `$PORT`; any host that runs
> `survey-agent serve --public --multi` works. See [HOSTING.md](HOSTING.md) for options.

---

# Appendix B — Reference

- **Skills** (repo `skills/`): `/author-survey /edit-survey /add-logic /test-survey /launch-survey
  /survey-report /inline-survey`.
- **CLI:** see [Appendix C](#appendix-c--command-cheat-sheet), or `survey-agent help`.
- **Service URLs:** `/` holding page · `/s/<slug>?uid=&rid=&group=` · `/chat?survey=<slug>` · `/health` ·
  `/api/responses[/<slug>][?format=csv]` (token via `Authorization: Bearer <token>` or `?token=`).
- **Storage — flat files by default, a database when you need one:** unset `SURVEY_STORE` = `file` →
  `<SURVEY_DATA_DIR>/responses/<slug>.jsonl` (default `./survey_data`); switch with one env var:
  `SURVEY_STORE=sqlite`, `SURVEY_STORE=mongo` + `SURVEY_MONGODB_URI=…`, or your own
  `pkg.module:Store`. No code change; surveys stay files in git.
- **Two Render blueprints (pick one — Render reads `render.yaml`, so `cp` your choice over it). Both
  request the paid Starter plan as shipped:**
  - `render.yaml` — **simple / low-traffic.** Flat files on a 1 GB `/var/data` disk. NOT zero-downtime
    (single disk-bound instance → stop-start on each deploy). Build `pip install ".[llm]"`.
  - `render.mongo.yaml` — **zero-downtime / continuous fielding.** Diskless + MongoDB. Rolling
    deploys. Build `pip install ".[llm,mongo]"`; set `SURVEY_MONGODB_URI` in the dashboard.
  Both: start `survey-agent serve --public --multi` · env `SURVEY_DIR`, `SURVEY_API_TOKEN`. The public
  base URL is derived from `RENDER_EXTERNAL_URL`.

---

# Appendix C — Command cheat sheet

Everything `survey-agent` does, grouped by where it falls in the loop. **New here? `survey-agent help`**
prints a guided overview of this flow (bare `survey-agent` shows it too). Run `survey-agent <cmd> --help`
for the full flags of any command.

### Author & build
| Command | What it does |
|---|---|
| `survey-agent author "<brief>" [--slug ID] [--out DIR]` | Draft a survey from a plain-language brief → writes `<slug>.survey.json` + `.md` (a model key gives real drafts). |
| `survey-agent new <template> --slug <id> [--param K=V]` | Scaffold a new survey from a template. |
| `survey-agent templates [--type T] [--json]` | List available templates. |
| `survey-agent copy <source> --slug <id>` | Copy an existing survey under a new id. |
| `survey-agent templatize <source> [--id ID]` | Turn a survey into a reusable template. |
| `survey-agent compile <file>.md [--write]` | Markdown → Survey JSON. |
| `survey-agent render <file>.survey.json [--write]` | Survey JSON → readable, logic-annotated Markdown. |
| `survey-agent schema` | Print the canonical Survey JSON Schema. |
| `survey-agent init [DIR]` | Scaffold a project (config + `.env` + example) in a folder. |

### Check before launch
| Command | What it does |
|---|---|
| `survey-agent validate <survey> [--json]` | Schema + semantic lint (every option/branch/quota/token resolves). |
| `survey-agent critique <survey> [--json]` | Methodology review — leading/double-barreled questions, missing opt-outs, bad ranges. |
| `survey-agent simulate <survey> [-n 500] [--json]` | **Coverage gate** — N synthetic respondents; incidence, drop-out, LOI, unreached paths. |
| `survey-agent pretest <survey> [--n N] [--audience F]` | Persona pre-test with synthetic personas: realistic incidence preview. |
| `survey-agent selftest [survey]` | End-to-end self-check of the install on a survey (default: the bundled example). |

### Preview & serve
| Command | What it does |
|---|---|
| `survey-agent serve [--multi --dir surveys] [--port 8000]` | Run the runtime — one survey, or every survey in a dir at `/s/<id>`. |
| `survey-agent serve --public …` | Production mode: binds `0.0.0.0`, serves only `live` surveys (`SURVEY_SERVE_STATES`). Store and integration are whatever you configured (defaults `file` / `none`). |
| `survey-agent serve --preview …` | Local draft preview: serves every lifecycle state on loopback only (refused with `--public` or a non-loopback `--host`). |
| `survey-agent demo` | Local sandbox: runtime (:8000, every lifecycle state) + a mock receiver (:9000); responses to `./survey_data`. |
| `survey-agent convo [--survey F] [--scripted …]` | Take a survey as a conversation in the terminal. |
| `survey-agent console [--dir surveys]` | Local operator console on `127.0.0.1` (unauthenticated). |
| `survey-agent api [--host H] [--port P]` | API gateway (`[api]` extra) — **open unless you configure auth**. |

### Launch & links
| Command | What it does |
|---|---|
| `survey-agent launch <survey> [--base URL] [--approve TOKEN] [--ack-responsibilities] [--deploy]` | Emit the drop-in `survey_url` and mark the survey live (**approval-gated** when `SURVEY_LAUNCH_APPROVAL_TOKEN` is set). Marking live is *local* — it does **not** publish; `--deploy` also runs your configured deploy hook (`deploy.command` / `$SURVEY_DEPLOY_COMMAND`). `--no-live` just prints the URL. |
| `survey-agent sign-url <survey> uid=… rid=… [group=…] [--base URL]` | Mint a signed per-respondent URL (HMAC with `SURVEY_URL_SECRET`). |
| `survey-agent publish <survey>` | With a runtime definition store: validate → store → live, no redeploy (same approval gate). |

### Operate, pull & analyze
| Command | What it does |
|---|---|
| `survey-agent list [--json]` | Every survey + lifecycle state. |
| `survey-agent status <id> [--set STATE] [--url …] [--note …] [--project …]` | Show or set one survey's state (`draft\|validated\|tested\|approved\|live\|paused\|closed`) / live URL / note / project. **`--set paused` or `closed` stops fielding**; `--set live` resumes. |
| `survey-agent dashboard [--live --base-url …] [--reports] [--simulate N] [--open]` | Self-contained HTML ops dashboard; `--live` adds URL + params + live counts per survey. |
| `survey-agent report <survey> [--insights] [--open]` | HTML report — completes, incidence, median LOI, screen-out/quota counts, cross-tabs. |
| `survey-agent insights <survey>` | AI analyst read-out: headline, findings, open-text themes, next steps. |
| `survey-agent export <survey> [--format csv\|parquet] [--out …]` | Raw responses for your own BI tooling (not redacted). |
| `survey-agent backup [--survey ID] [--out F]` / `restore <file>` | Portable JSONL backup of stored responses / reload into any store. |
| `survey-agent defs-export [--all]` / `defs-import` / `defs-diff` | Back up, rebuild, and reconcile a runtime definition store. |
| `survey-agent dsar <uid> [--survey ID] [--delete] [--yes] [--json]` | Find a subject's responses; `--delete` erases them from the store. |
| `survey-agent outbox status\|list\|retry [--dead]` | Inspect/retry the durable outbox for webhook actions (`SURVEY_OUTBOX=1`). |
| `survey-agent docs [name] [--open]` | List or open the bundled docs. |
| `survey-agent help` | Guided overview of the commands + the author→launch→report flow. |
| `survey-agent doctor [--json]` | Diagnose the install + config + deployment checks; shows the resolved public URL; flags the multi-clone trap. |

> Analyzing **production** data with `report` / `insights` / `export` / `dashboard --live`? They read your
> **local** `SURVEY_STORE` — point it at the production store first (Step 6), or pull over the API instead.

### Service endpoints (the live runtime)
| Route | What it does |
|---|---|
| `GET /` | Holding page (lists surveys only with `SURVEY_LANDING_LIST=1`). |
| `GET /s/<id>?uid=&rid=&group=&lang=` | The drop-in survey page; all query params → `url_variables`. |
| `GET /chat?survey=<id>` | The conversational surface for a survey. |
| `GET /health` | `{"ok": true}` — uptime/health check. |
| `GET /api/responses[/<id>][?format=csv]` | Token-gated data pull (`Authorization: Bearer <token>` or `?token=`); disabled until `SURVEY_API_TOKEN` is set. |
| `GET /manage` | Management console — opt-in `SURVEY_CONSOLE=1`, **unauthenticated**. |

### Key environment variables
| Var | What it sets |
|---|---|
| `SURVEY_STORE` | `file` (default when unset) \| `sqlite` \| `mongo` \| `http` \| `memory` \| `pkg.module:Store`. |
| `SURVEY_DEFINITION_SOURCE` | Where definitions + lifecycle live: `file` (default) \| `mongo` \| `http` \| `pkg.module:Defs`. Non-file = publish/pause are store writes, no redeploy. |
| `SURVEY_DATA_DIR` | File-store directory (default `./survey_data`). |
| `SURVEY_SQLITE_PATH` | SQLite file for `SURVEY_STORE=sqlite`. |
| `SURVEY_OUTPUT_DIR` | Folder for generated reports / dashboards / exports (also `output.dir` in `survey-agent.config.yaml`). An explicit `--out` wins. |
| `SURVEY_MONGODB_URI` / `SURVEY_MONGODB_DB` | Mongo connection + database name. |
| `SURVEY_UPLOAD_STORE` | Where `file`-answer bytes go: `local` \| `s3` \| `pkg.module:Store` (default `local`). |
| `SURVEY_UPLOAD_DIR` / `SURVEY_UPLOAD_BUCKET` | Local dir (local) / S3 bucket (s3, needs the `[s3]` extra + cloud credentials). |
| `SURVEY_DIR` | Surveys folder served by `--multi`. |
| `SURVEY_SERVE_STATES` | Lifecycle states served to respondents (default `live`). |
| `SURVEY_PREVIEW_TOKEN` | Enables operator preview of pre-live surveys via `?preview=<token>`. |
| `SURVEY_API_TOKEN` | Gates `/api/responses` (unset → the endpoint is disabled); also the gateway's bearer token. |
| `SURVEY_PUBLIC_BASE_URL` | Optional override for the public base in emitted URLs (otherwise derived from the host; `doctor` shows it). |
| `SURVEY_LAUNCH_APPROVAL_TOKEN` | When set, marking live on any surface requires this token. |
| `SURVEY_URL_SECRET` | HMAC secret for signed respondent URLs (`sign-url` + runtime verification). |
| `SURVEY_INTEGRATION` | Terminal passback: `none` (default) \| `redirect` \| `webhook` \| `panel` \| `mailchimp` \| plugin. |
| `SURVEY_WEBHOOK_SECRET` | HMAC secret signing outbound webhook posts. |
| `SURVEY_REDIRECT_{COMPLETE,TERMINATED,OVERQUOTA}_URL` | When `=redirect`: per-status URL the browser is sent to at a terminal. |
| `SURVEY_REDIRECT_PASSBACK` | url_variables appended to the redirect (e.g. `uid,rid`). |
| `SURVEY_PANEL_{STARTED,COMPLETED,TERMINATED,TERMINATED_BY_USER}_URL` | When `=panel`: per-event callback URLs. |
| `SURVEY_PANEL_SHARED_SECRET` | HMAC secret signing the `panel` passback calls. |
| `SURVEY_OUTBOX` | `1` = durable retry/dead-letter queue for webhook **actions** (not terminal passbacks). |
| `SURVEY_ENRICH` | Behavioral enrichment: `static` \| `firebolt` \| `your.pkg:Provider`. |
| `SURVEY_LLM_ENRICH` | `1` = allow opt-in `source: llm` analysis at completion. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Model for authoring / conversation / analysis. |
| `SURVEY_CONSOLE=1` | Enable the `/manage` console on the runtime. |

#### Browser-redirect passback (`SURVEY_INTEGRATION=redirect`)
A common panel pattern: at a terminal, **redirect the respondent's browser** back to the panel with
ids on the query string — completer → complete URL, screen-out → terminate URL. Set:

```
SURVEY_INTEGRATION=redirect
SURVEY_REDIRECT_COMPLETE_URL=https://panel.example.com/complete
SURVEY_REDIRECT_TERMINATED_URL=https://panel.example.com/terminate   # disqualify (+ over-quota unless OVERQUOTA is set)
SURVEY_REDIRECT_PASSBACK=uid,rid                                      # appended as ?uid=…&rid=…
```

The redirect fires on **every** terminal (complete, screen-out, over-quota), not just complete. A
survey overrides any terminal with a spec redirect of kind `url` (e.g. to send one survey to a
different panel); the env URLs are the default. Use this when the panel expects a browser landing;
use `panel` (below) when it expects a server-to-server call.

#### Panel passback (`SURVEY_INTEGRATION=panel`)
A generic, reference server-to-server passback: select the `panel` integration and point the
`SURVEY_PANEL_*_URL` vars at your endpoints:

| Lifecycle event | Fired on | Env var |
|---|---|---|
| `started` | respondent begins | `SURVEY_PANEL_STARTED_URL` |
| `completed` | qualified completion | `SURVEY_PANEL_COMPLETED_URL` |
| `terminated` | disqualify **or** over-quota | `SURVEY_PANEL_TERMINATED_URL` |
| `terminated_by_user` | respondent abandons — **not fired by the stdlib runtime** (it has no abandon detection); available to custom hosts | `SURVEY_PANEL_TERMINATED_BY_USER_URL` |

Each call POSTs the neutral event — it carries `url_variables` (so whatever ids your panel put on the
link ride along), the response's reason code, a stable `idempotency_key` for dedupe under retry, and
an `X-Survey-Signature` HMAC over the body keyed by `SURVEY_PANEL_SHARED_SECRET`. Leave a URL blank to
skip that event; set `SURVEY_PANEL_CALLBACK_METHOD=GET` to send the event as a query string instead.
Passbacks are best-effort (the outbox does not cover them); reconcile against the store if a lost
callback matters. The endpoint URLs are config — no panel/vendor name lives in the engine.

#### Where definitions live (`SURVEY_DEFINITION_SOURCE`)
By default (`file`) survey definitions are `.survey.json` files in `SURVEY_DIR` and lifecycle is the
`.registry.json` sidecar — publishing or pausing means a git push → redeploy. Point it at a **runtime
store** to decouple that (`mongo` is the reference backend, reusing `SURVEY_MONGODB_URI`/`DB`; `http`
goes through the API gateway; or bring your own via an import path):

```
SURVEY_DEFINITION_SOURCE=mongo        # definitions + lifecycle in the database, version-pinned
```

Then **publishing and pause/close are store writes — no redeploy**, and in-flight respondents keep the
version they started on. The local files stay the authored artifact + a readable, git-backed backup:

| Command | Does |
|---|---|
| `survey-agent publish <file>` | validate → store the definition (auto-bumps the version on a content change) → mark live (approval-gated) |
| `survey-agent status <id> --set paused\|closed\|live` | lifecycle change, no redeploy |
| `survey-agent defs-export [--all]` | dump the store → files (cold backup; `--all` = every version) |
| `survey-agent defs-import` | rebuild the store from files (store-loss recovery) |
| `survey-agent defs-diff` | reconcile local files vs the store's live versions |

With the default `file` backend, `publish` points you at your deploy step instead.

### Survey-level settings worth knowing
Set inside the survey JSON under `settings` (the authoring/`/edit-survey` skills write these; `validate` gates them):
| Setting | What it does |
|---|---|
| `retake` | `"once"` (default) — a finished invite link (uid+rid) is locked and shows an already-completed screen. `"multiple"` — returning after finishing starts a **fresh attempt**; each completion is kept as its own response with its own id and passback. |
| `identity` | Which URL vars carry identity — `{respondent, response}`; defaults to `uid`/`rid`. |
| `url_signing.required` | Reject links whose signed params are missing or edited (see `sign-url`). |
| `theme` | Visual theme for the survey pages (`dark`, `ocean`, `paper`, or your own via `SURVEY_THEMES_DIR`). |
| `anti_fraud` | Declared and reference-checked, but **not enforced by the engine** yet — write an attention check as an ordinary disqualify rule. |

---

# Appendix D — Authoring from Claude Desktop (cowork / code)

The loop above is shown on the terminal, but not everyone on a research team lives in one. Here's how
a teammate works inside a desktop agent app (the example is the Claude Desktop app; any MCP-capable
client works similarly) against the same `my-surveys` repo + hosted runtime.

**Pick the surface:**
- **Code** (has a terminal + files) → nothing special: run the `survey-agent` CLI exactly as in Steps
  1–8. The MCP connector below is optional.
- **Cowork** / **chat** (tools + files, *no terminal*) → survey-agent runs as MCP **tools**. With file
  access, `save_survey` writes a real `surveys/<slug>.survey.json` into your clone, so it feeds the
  same PR → merge → deploy loop. This is the no-terminal path.

### In the *code* surface (terminal + agent) — nothing extra to set up
The code surface gives the agent a full terminal + file access in your project, so there's nothing
survey-agent-specific to configure. Open your `~/my-surveys` folder and either run the CLI yourself
or ask the agent to drive it — every command from Steps 1–8 applies verbatim:
```bash
survey-agent doctor                                  # confirm this clone's code is what runs (multi-clone check)
survey-agent author "AI-at-Work screener: AI use at work, which tools, time saved, role"
survey-agent validate  surveys/ai-at-work.survey.json
survey-agent simulate  surveys/ai-at-work.survey.json -n 500
survey-agent serve --preview --multi --dir surveys   # preview at /s/ai-at-work
survey-agent launch    surveys/ai-at-work.survey.json   # URL + mark live (publish via the PR/deploy below)
```
…or in plain language: *"author an AI-at-Work screener, validate + simulate it, then show me the preview
URL"* — the agent runs the same commands. Ship it the same way: commit on a branch → PR → merge →
deploy.

### In the *cowork* surface — one-time per teammate, wire the connector to *your* clone
```bash
cd ~/my-surveys
pip install -e ".[llm,mcp]"           # add `mongo` if your deployment uses it; note the `mcp` extra
command -v survey-agent-mcp           # copy this absolute path
```
Then register it in the app's MCP config (in Claude Desktop: **Settings → Developer → Edit Config**),
adding this **inside** the existing `mcpServers` object (template: [`.mcp.json.example`](../.mcp.json.example)):
```json
"survey-agent": {
  "type": "stdio",
  "command": "/ABSOLUTE/PATH/to/my-surveys/.venv/bin/survey-agent-mcp",
  "env": { "SURVEY_DIR": "/ABSOLUTE/PATH/to/my-surveys/surveys" }
}
```
Point `command` and `SURVEY_DIR` at **the my-surveys clone you open PRs from** (the same multi-clone trap
`survey-agent doctor` guards against — here the connector, not the CLI, has to resolve to the right
checkout). Don't put your API key here; the server loads the `.env` next to `SURVEY_DIR`. Note the MCP
connector can never mark a survey live without `SURVEY_LAUNCH_APPROVAL_TOKEN`; a human supplies it
when approving a launch. Then **fully quit and reopen the app**; the tools control in the message box
lists `survey-agent`'s tools.

### The loop in cowork (worked example)
Open your `~/my-surveys` folder, then describe what you want — the agent drafts the JSON and runs the
tools:

> *"Draft a survey-agent screener for the **AI-at-Work** study (read `survey://schema` first): do you
> use AI assistants for work, which ones, time saved, your role. Give it a one-line description + tags,
> **save it**, then run the coverage gate and critique it."*

The agent reads the schema → `validate_survey` → **`save_survey`** (writes `surveys/ai-at-work.survey.json`
into your clone) → `simulate_survey` → `critique_survey`. Then:
- *"Pre-test the incidence / who screens out"* → `pretest_survey` (synthetic personas → incidence + reasons).
- *"List my surveys / build a dashboard"* → `list_surveys` / `build_dashboard` (writes `dashboard.html`).
- *"Move ai-at-work to the Q3 project / mark it tested / paused"* → `set_survey_status` (state, note,
  `project`). Going **live** needs the approval token + acknowledgment.
- *"Launch ai-at-work"* → `launch_survey` returns the drop-in `survey_url`; `live` only flips with the
  approval token. **It does not publish** (see below).
- *"How did it do / read out the open-ends / export the data"* → `survey_report` · `survey_insights` ·
  `export_survey` (CSV/JSON grid). These read the configured store — i.e. the agent and its model
  provider see real response data — or pass `simulate=N` for synthetic rows.

From there it's the **same git loop as Step 5**: the saved file is in your clone, so commit it on a
branch → PR → a teammate reviews → merge to `main` → the host deploys → live. Two reminders: there's
**no `author` tool** (drafting is the agent's job; describe the survey), and **`launch_survey` marks
live, it doesn't publish** — the deploy (merge/push) is what makes it reachable.

### Team shapes
- **Per-teammate local connector (above)** — each person authors in their own clone and ships via PR.
  Best fit for the reviewed-deploy model; nothing extra to host.
- **One shared hosted connector** — for a web chat client (no local files at all) or a single team
  endpoint, run `survey-agent-mcp --transport streamable-http` behind HTTPS + **OAuth you provide** and
  have everyone add it as a custom connector by URL. Files then live on the connector host, not a
  teammate's clone — so pair it with a server-side commit/push if you want the PR loop. See
  [HOSTING.md](HOSTING.md#hosting-the-remote-mcp-connector-chat--cowork-on-claudeai).
