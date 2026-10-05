# survey-agent: User Guide

This guide is for **the person running surveys**: building a screener, testing it, launching it to
your respondents, and watching it field. You don't need to write code. You work through **Claude
skills** (typed as `/commands` in Claude Code) and review plain files in a pull request. Think of
Claude as the survey builder and yourself as the editor who signs off.

> Mental model: survey-agent is **the survey-hosting piece** of your stack. Whatever system sends
> people to the survey (a panel, a CRM, an email tool, your own app) still decides who gets invited,
> manages any quotas across that audience, and pays any incentives. survey-agent owns *the questions,
> the logic, and the routing*, and you approve it before it goes live.

> **Just want to try it first?** [QUICKSTART.md](QUICKSTART.md) walks you from install through
> validating, simulating, and taking a survey in your browser (and as an AI chat) in a few minutes,
> with no setup.

> **Before you field to real people**, the instance you're launching into must be set up responsibly
> (durable store, an erasure path, consent text, HTTPS). That's a one-time technical setup; see the
> [operator responsibilities](OPERATOR_RESPONSIBILITIES.md). `launch` prints that checklist, and a
> token-gated deployment asks you to acknowledge it before a survey goes live.

---

## The lifecycle at a glance

```
  brief ─▶ /author-survey ─▶ /edit-survey, /add-logic ─▶ /test-survey ─▶ review PR ─▶ /launch-survey ─▶ /survey-report
  (you)        (Claude)            (you + Claude)           (Claude)        (you)         (you + Claude)      (you)
```
There is always a human checkpoint before anything goes live to real people.

The seven skills in `skills/`:

| Skill | What it does |
|---|---|
| `/author-survey` | Turn a plain-language brief into a validated survey file |
| `/edit-survey` | Add, remove, reword, or reorder questions; change options, quotas, or theme |
| `/add-logic` | Skip/branch logic, screen-outs, piping, repeating, enrichment, quotas, routing |
| `/test-survey` | Simulate respondents through every path (the coverage gate) |
| `/launch-survey` | Emit the drop-in `survey_url` and mark the survey live |
| `/survey-report` | Fielding stats and question-level analytics |
| `/inline-survey` | Run a survey inline in an agent conversation (`begin_survey` / `answer_survey` MCP tools) to collect structured answers from a person mid-task |

---

## See what you have: `survey-agent list`

As you build more than one survey, this is your control panel. Ask Claude to *"list my surveys"* (or
run `survey-agent list`; add `--json` for machine-readable output) and you get every survey in the
project at a glance: its **purpose**, its **state** in the lifecycle, and whether it's been tested.

```
2 survey(s) in surveys/

  🟢 streaming-usage         live           inc 47% · 2.4m · url↗
        Streaming Services Usage — UK current subscribers
  🔎 mobile-gaming-screener  validated
        UK weekly mobile gamers — screener for the gaming task
```

- **State** moves on its own as you work: `draft` → `validated` (passed the schema gate) → `tested`
  (passed the coverage simulation, with its incidence/length) → `approved` → `live`. You can also set it
  by hand, *"mark streaming-usage as live"* (`survey-agent status streaming-usage --set live --url
  <survey_url>`), or `paused` / `closed` when it's done.
- **`live` is the gate, not just a label.** A production runtime (`survey-agent serve --public`) serves
  real respondents **only when a survey is `live`**. While it's still `draft`/`validated`/`tested`/
  `approved`, respondents get a *"not open yet"* page; only an operator can **preview** it (the
  console's *open as respondent* link, which collects no data). Marking live also **pins the version**
  that went out, so a later edit doesn't change what in-field respondents see until you re-publish.
  (During a migration you can temporarily widen the gate with `SURVEY_SERVE_STATES`; see below.)
- **Purpose travels with the file.** Give each survey a one-line `description` (and optional `tags` /
  `owner`) when you author it (*"...and describe it as the Q3 streaming screener, tag it streaming"*),
  so six months later you know what `streaming-usage` actually was.
- **Group surveys into projects.** Give each survey an optional **`project`**, a free-text
  organizational group (e.g. "Q3 tracking", "Welcome onboarding"), and move it freely:
  *"move `streaming-usage` to the Q3 project"* or `survey-agent status streaming-usage --project "Q3"`
  (`--project ""` to ungroup). It's mutable metadata in the registry, **not** part of the definition, so
  regrouping never bumps the version. The console board collapses by project; the distinct values *are*
  the projects (create one by naming it; it disappears when its last survey leaves). Use projects as
  folders and `tags` for cross-cutting descriptive labels.
- State lives in a small `.registry.json` next to your surveys (it never touches the frozen survey
  definitions). Commit it if you want the whole team to share status; leave it local otherwise.

`survey-agent status <id>` shows one survey's full record (when it was tested, the incidence, where
it's live, any note you left). The same view is available to an agent over MCP as `list_surveys`.

**Pause or close fielding:** `survey-agent status <id> --set paused` stops the survey: respondents see a
"paused" page and no new sessions start (anyone mid-survey may still finish). `--set live` resumes it,
and `--set closed` stops it for good. (With the default file-based definitions on a hosted runtime,
commit the registry change and redeploy for it to take effect; with a runtime definition store it
applies immediately.)

**Rolling out the gate / migrating existing surveys:** because only `live` serves, a survey you never
marked live won't field. To onboard a batch without flipping each one first, set
`SURVEY_SERVE_STATES=live,approved,tested` (comma-separated states the runtime will serve) on the
runtime during the transition, then mark surveys live properly and tighten it back to the default
(`live`). To preview a not-yet-live survey on a shared runtime, set the same `SURVEY_PREVIEW_TOKEN` on
the runtime and the console; the console's local preview runtime mints one automatically.

**Want it visual?** *"Build me a dashboard"* (`survey-agent dashboard --reports --open`) writes a
single self-contained HTML page: a card per survey with its state, last incidence/LOI, and a link
straight into each survey's report. Add **`--live`** (with `--base-url`) to also show each survey's
drop-in URL, accepted params, and **live counts** (completes / incidence / quota) pulled from the store.
It's just a file (open it, commit it, share it). For an interactive operator view, run
`survey-agent console` (see [CONSOLE.md](CONSOLE.md)). The runtime also has a read-only `/manage`
page, off by default; enable it with `SURVEY_CONSOLE=1` and keep it behind your own auth if you host it.

---

## 1. Describe what you want: `/author-survey`

Give Claude a plain-language brief. The more you say up front, the fewer questions it asks back.
A good brief covers: **who** to screen, **what disqualifies** them, **how many** you need (and any
quota cells), roughly **how long**, **where qualifiers go next** (a follow-up task or URL), and any
**behavior** you want to ask about.

> `/author-survey  Screen UK adults who currently use streaming services (Netflix/Disney+/Max). Cap
> current users at 300. About 3 minutes. Add an attention check. Route people who qualify into the
> streaming follow-up task. If there's a recent streaming search on record for them, ask them why.`

Claude will ask anything missing (e.g. "which theme: paper, dark, or ocean?"), then produce two files:
- `surveys/streaming-usage.survey.json`, the machine-readable survey (the source of truth)
- `surveys/streaming-usage.md`, a **readable version you can actually read and review**

and print a flow summary like:
```
p_intro    consent           → "No" screens out (no_consent)
p_screen   age, streaming usage   → "Never used" screens out (non_user)
p_brands   which brands       (anyone who's used one)
p_behavior "we saw you search for {their search} — why?"   (only if a streaming search is on record)
p_detail   best brand (from what they picked) + attention check   (current users)
quota      current users capped at 300 → over-quota
complete   route → streaming follow-up task
```

**Read the `.md`.** If the flow matches your intent, move on. If not, fix it in step 2.

---

## 2. Adjust it: `/edit-survey` and `/add-logic`

Just say what to change, in plain English:

> `/edit-survey  Reword the consent question to say it's a 3-minute study.`
> `/add-logic  Also screen out anyone under 18, and add a quota of 150 for past-6-month users.`
> `/add-logic  Pipe their selected brand into the "which works best" question.`

A few things worth knowing (Claude handles them, but so you understand the output):
- **A question that depends on a previous answer goes on a *later page*.** That's why "which brands"
  and "why did you search" are their own pages: the survey shows one page at a time. (`validate`
  rejects a question gated on an answer from the same page, so this can't slip through.)
- **Every screen-out has a reason** (you'll see `no_consent`, `non_user`, `failed_attention`). These
  show up in your report so you know *why* people dropped.
- **Piping** is how you reuse an answer (or a URL value, or a context fact) later:
  `{{q:q_services.answer}}`, `{{url:group}}`, `{{ctx:recent_stream_search}}`. If the value might
  be missing (an optional question, a page not everyone sees), give it a fallback:
  `{{q:q_fav.answer|your favourite}}`. `validate` warns when one is needed, and `simulate` tells you
  if anyone would have seen a blank. `surveys/piping-showcase.survey.json` in the repo shows every form.
- **Repeating** asks the same thing once per item someone picked:
  > `/add-logic  For each service they use, ask how satisfied they are, then ask what they love about the ones they rated 5.`

  The page (or question) gets a `repeat` pointing at the earlier question, and `{{loop:label}}` puts
  the item's name in the wording ("How satisfied are you with Netflix?"). Each item's answer is saved
  under its own name, like `q_sat[netflix]`, so the report and CSV have one column per service. A
  follow-up that depends on that answer goes on a second repeated page, the same later-page rule as
  above. "None of the above" is never repeated. For a worked example, see
  `surveys/streaming-brand-ratings.md` in the repo.
- **Attention checks** are ordinary questions plus a disqualify rule (e.g. "select *Strongly agree*",
  anything else screens out as `failed_attention`). `settings.anti_fraud` can be declared in a spec,
  but the engine doesn't enforce it yet, so write the rule explicitly. `settings.retake` (`once` by
  default, or `multiple`) *is* enforced.

### Show a video, or notify another system

Two extras you can ask `/add-logic` for in plain English:

- **Video stimulus**, *"show this YouTube clip before the first question, then ask what they thought."*
  A `video` is **display-only**: it's shown, never answered, and never blocks the page. Paste a YouTube
  link (it renders in privacy mode, no tracking cookies) or any direct video URL, then put the
  question(s) about it on the **next** page. (`image` and `instructions` work the same way.)
- **Webhook (notify on a condition)**, *"when someone qualifies, POST their answer to my CRM."* A
  `webhook` action fires **once** per respondent when its condition is true, sending a small JSON
  payload (with merge tokens like `{{q:…}}`/`{{url:uid}}` filled in) to a URL you control. Keep the
  payload minimal and use an endpoint you trust (see [DATA_SECURITY.md](DATA_SECURITY.md)). It never
  blocks or slows the respondent. Delivery is best-effort by default; set `SURVEY_OUTBOX=1` on the
  runtime for a durable outbox with retries (`survey-agent outbox` inspects it).

---

## 3. Test before you field: `/test-survey`

> `/test-survey  streaming-usage`

This sends **synthetic respondents** down every path and reports what happened:
```
Terminal status:
  disqualified       370  (74.0%)
  completed          130  (26.0%)

Screen-out reasons:
  no_consent           245
  non_user              64
  failed_attention      61
...
✅ coverage gate passed: complete + every declared screen-out reachable; all callbacks mapped.
```
**What to look for:**
- Does the **complete rate (incidence)** look roughly right for who you're targeting? If you expected
  ~25% to qualify and it says 2%, your screen-out logic is too tight.
- Are the **screen-out reasons** the ones you intended?
- Does it say **coverage gate passed**? If it fails, the survey isn't ready to launch, and Claude will
  tell you what's wrong (a dead end, a missing route, a typo in logic).

**See it for real:** ask Claude to run the local demo (`survey-agent demo`) and click through
a couple of paths yourself in the browser. A mock receiver prints each lifecycle event (started,
completed, screened out) as it happens, which is what your own system would receive in production.

---

## 4. Sign off (the human gate)

The two survey files are committed to a branch and opened as a pull request. **Review the diff.**
The `.md` reads like a normal questionnaire. Approve it when you're happy. **Nothing is fielded until
a person approves.** If your deployment sets `SURVEY_LAUNCH_APPROVAL_TOKEN`, marking a survey live
requires that token on every surface (CLI, console, MCP); the MCP connector always requires it.

---

## 5. Launch: `/launch-survey`

> `/launch-survey streaming-usage`

This:
1. **Marks the survey live and freezes the version** (so people taking it mid-stream are never
   affected by later edits). This is *local* bookkeeping in the registry; by itself it does **not**
   publish the survey. With an approval token configured, the CLI form is
   `survey-agent launch <file> --approve <TOKEN> --ack-responsibilities`.
2. Gives you the **drop-in survey URL** to hand to your panel / CRM / email tool:
   `https://survey.example.com/s/streaming-usage?uid={uid}&rid={rid}&group={group}`
   Replace each `{placeholder}` with your system's value per respondent. If your system uses other
   parameter names (e.g. `pid`, `panel_id`), set `settings.identity` in the survey and everything
   (session key, dedup, enrichment lookup, erasure) follows it.
3. **Publishing is a separate, explicit step**, and which step depends on `SURVEY_DEFINITION_SOURCE`:
   - **File backend (default):** the survey is reachable once your hosted runtime is *deployed* with its
     file: `git push` to your deploy repo (or `survey-agent launch --deploy`, which runs your configured
     `deploy.command` / `$SURVEY_DEPLOY_COMMAND`).
   - **Runtime store (`mongo` / `http` / custom):** `survey-agent publish <survey.json>` stores the
     definition and marks it live **with no redeploy** (version-pinned; the file stays the backup).

   Plain `launch` prints the exact publish step it detects. See [DEPLOY.md](DEPLOY.md).
4. When respondents finish, the runtime sends **neutral lifecycle events** (`started`, `completed`,
   `terminated`, plus a reason code) through whichever integration you configured
   (`SURVEY_INTEGRATION`: `redirect`, `webhook`, the generic `panel` passback, `mailchimp`, or your own
   plugin). Unset means no passback. survey-agent itself never recruits or pays anyone.

**Replacing a survey hosted elsewhere?** Tell Claude. It rebuilds it as a survey-agent survey; you run
both side by side for a soak period to confirm the complete/screen-out rates match, then retire the
old copy. Low risk and reversible (it's just a URL).

---

## 6. Watch it field: `/survey-report`

> `/survey-report  streaming-usage`

You get completes, screen-outs, over-quota, **incidence**, **length of interview**, and question-level
breakdowns and cross-tabs, rendered as a clean HTML report (`survey-agent report <file> --open`).
Add `--insights` for an AI analyst summary (headline, findings, open-end themes). Use it to spot
problems early (incidence way off, a quota cell filling too fast). Spend and invitation counts live in
whatever system sends people to the survey; combine them yourself if you need cost figures.

---

## Behavior-aware surveys (enrichment)

If you have behavioral or profile data about your respondents (in a warehouse, a CRM, a feature
store), survey-agent can **use it in the survey**. You declare the fact you want, and Claude does the
wiring via `/add-logic`:

- *"Most recent streaming search in the last 30 days"* → pipe it into a question: *"We noticed you
  searched for **{their term}**, what were you hoping to learn?"*
- *"Minutes in health & fitness apps in the last 7 days"* → branch on it or tag high-engagement people.
- *"Did they buy brand X in the last 90 days?"* → only ask the follow-up if yes.

Enrichment values can drive logic, piping (`{{ctx:…}}`), and routing. They're fetched when a
respondent **starts** the survey (not for everyone who's merely invited), so they're ready by the page
that needs them. Out of the box survey-agent ships a `static` provider (fixtures, for demos and tests)
and a reference warehouse adapter; connecting your own data source means writing a small adapter
(see [ENGINEERING.md](ENGINEERING.md#6-behavioral-enrichment-your-own-data)). That's a one-time
technical task for whoever runs your deployment.

## Auto-launch on a behavior (triggers)

You can also have a survey **offered automatically when a behavior is seen**, e.g. *"when someone
searches for a streaming service, offer them the streaming screener so we can ask why while it's
fresh."* A trigger is a small file describing the behavior to watch and the (already approved) survey
to offer; the trigger sidecar sends a launch request to the system that invites people
(`PANEL_LAUNCH_URL`). The survey itself is always one a human approved first; the trigger just decides
*who* gets it and *when*. Enabling the sidecar is a deployment task; see
[ENGINEERING.md](ENGINEERING.md#7-trigger-sidecar).

---

## Conversational mode (the survey as a chat)

The *same* survey can be delivered as an AI conversation instead of a form, with no rework. An AI asks
each question in plain language and maps free-text replies to valid answers; the engine still enforces
all the logic, screen-outs, and quotas underneath. Try it offline:

```bash
survey-agent convo --scripted yes 34 "Currently subscribe" Netflix "Checking whether the price went up" Netflix "Strongly agree"
```

That walks the bundled example to `[survey terminal: completed]` with the offline stub. Set
`ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) for natural phrasing. Conversation mode works well for
single-select, multi-select, number, and text questions. **Grids, rankings, and file uploads aren't
supported in conversation mode**; use the form for surveys that need them. Because an AI reads the
replies, disclose that to respondents in your consent text (see below).

## Hidden values & scoring

You can compute **hidden fields** that respondents never see but that get stored on the response,
e.g. a score or a segment derived from answers. Ask Claude: *"add a hidden score from Q3 and bucket
it into low/med/high."* These are usable in logic, routing, and **quotas** (e.g. quota by computed
segment), and appear in exports under `derived`.

## AI analysis of answers (opt-in)

survey-agent can use AI to **analyze open-text answers** (sentiment, theme, or an author-written
summary of the respondent). The analysis runs **when the response completes** and is **stored on the
response** (exported under `derived`). It is not shown to the respondent and doesn't drive live logic,
piping, or routing. It sends specific answer fields to an AI, so it is **off by default** and only runs
when (a) the deployment enables it (`SURVEY_LLM_ENRICH=1`) and (b) the survey opts in, with optional
respondent consent. **Every AI call is recorded** on the response (an audit entry: which fields were
sent, when, the model, the outcome). Default consent/disclosure wording ships in the package and can
be overridden per survey (`settings.ai`). Treat the consent text, and whether consent is required, as
a product/legal decision before fielding.

## Theming (look & feel)
Each survey picks a theme: **`paper`** (clean light report style), **`dark`**, or **`ocean`**, all
built on a shared base stylesheet. Say `/edit-survey use the dark theme` and preview it in the demo
with `?theme=dark`. To add your own, point `SURVEY_THEMES_DIR` at a folder of theme directories.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `/test-survey` says **coverage gate failed** | A path dead-ends or a screen-out has no reason. Claude prints the exact issue; usually a one-line `/add-logic` fix. |
| **Incidence way lower than expected** | Screen-out logic too strict, or a quota set too low. Check the screen-out reasons in the report. |
| **A behavior question shows blank** (`searched for ""`) | There's no matching data for that respondent in the window. The page should be **gated** so it only appears when the fact exists, or the pipe given a fallback. Ask Claude to gate it. |
| **People route to the wrong place** | The `complete` action's redirect URL is wrong; `/edit-survey` to fix the route. |
| **Survey edited but live respondents seem unaffected** | Correct: edits create a *new version*; in-flight respondents stay on the version they started. New starts get the new one after you re-publish. |
| **Respondents see "not open yet"** | The survey isn't `live`. Mark it live (`/launch-survey` or `survey-agent status <id> --set live`) and publish it. |
| **No completes showing in the report** | Confirm the survey is live and published, that the report reads the same store the runtime writes to (`SURVEY_STORE`), and that the link you handed out points at your hosted runtime. |
| **Your system isn't hearing about completes** | Check `SURVEY_INTEGRATION` is set (unset means no passback) and its URL/secret settings. `survey-agent doctor` shows the resolved config. |

## Glossary
- **uid / rid**: the default URL parameters for the respondent id and the response (or invitation)
  id. Rename them per survey with `settings.identity`.
- **screen-out / disqualify**: a respondent who doesn't qualify; they get a polite end screen.
- **over-quota**: a cell (or the whole survey) hit its cap; further matching respondents are closed out.
- **incidence**: % of starts that complete (how "findable" your target is).
- **LOI**: length of interview (median time to complete).
- **passback**: the lifecycle event survey-agent sends your system when someone starts, completes, or
  screens out, so your system can update its records.
- **enrichment**: a fact about a respondent from your own data, usable in the survey's logic and
  piping.
