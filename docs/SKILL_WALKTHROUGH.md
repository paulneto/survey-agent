# Walkthrough: build & test a survey with the skills

> The **quick tour** of the skills loop. For the full reference (every feature, lifecycle state,
> troubleshooting, and the launch/publish details) see [USER_GUIDE.md](USER_GUIDE.md) and the
> [docs map](README.md).

This shows the end-to-end agentic loop a survey operator runs from Claude Code to take a survey
from a one-line brief to a tested, launch-ready spec. The skills live in `skills/`; the truth lives in
files (`surveys/*.survey.json`) reviewed via pull request.

The loop: **brief → draft → validate → simulate → human approves → launch → report.** Claude does
draft/validate/simulate/report; a human owns the approve → launch gate, before anything reaches real
respondents.

The seven skills: `author-survey`, `edit-survey`, `add-logic`, `test-survey`, `launch-survey`,
`survey-report`, and `inline-survey` (run a survey inside an agent conversation to collect structured
answers from a person mid-task; see the last section).

---

## 1. Author from a brief: `/author-survey`

```
/author-survey  Screen UK adults who currently use streaming services (Netflix/Disney+/Max),
                cap current users at 300, ~3 min, attention check, and route qualifiers into
                the streaming follow-up task. If there's a recent streaming search on record, ask why.
```

Claude asks only what's missing (quota cells? theme? task URL?), then writes
`surveys/streaming-usage.survey.json` plus a readable `surveys/streaming-usage.md`, and runs the
validator. You get a flow summary:

```
p_intro   consent (No → DQ no_consent)
p_screen  age 18–99 · streaming usage [shuffled, "Never" last]  (Never → DQ non_user)
p_brands  brands used                                        (shown to any user)
p_behavior  "We noticed you searched for {{ctx:recent_stream_search}} — why?"   (only if a streaming search is on record)
p_detail  best brand (piped from p_brands) · attention check (wrong → DQ failed_attention)  (current only)
quota     current users capped at 300 → over-quota
complete  route → app.example.com/task/streaming-followup?uid=…&rid=…&seg=…&engaged=…
```

## 2. Add or change logic: `/edit-survey`, `/add-logic`

```
/add-logic  Also disqualify anyone under 18, and add a quota of 150 for past-6-month users.
/edit-survey  Reword the consent question to mention it's a 3-minute study.
/add-logic  For each brand they use, ask how satisfied they are.
```
Each re-validates and shows a diff. (Behind the scenes these enforce: no dead branches, every
screen-out has a `reason_code`, every survey has a terminal `complete`, every referenced URL variable
and enrichment is declared, and no question is gated on an answer from its own or a later page.) The
"for each brand" one becomes a page that `repeat`s once per brand picked, storing each answer as
`q_sat[netflix]` and so on; see `surveys/streaming-brand-ratings.md` in the repo for a worked example.

## 3. Test before launch: `/test-survey`

```
/test-survey  streaming-usage
```
Runs `survey-agent simulate surveys/streaming-usage.survey.json --n 500`:

```
Terminal status:
  disqualified       370  (74.0%)
  completed          130  (26.0%)

Screen-out reasons:
  no_consent           245
  non_user              64
  failed_attention      61

Panel passbacks that would fire:
  terminated           370
  completed            130
...
✅ coverage gate passed: complete + every declared screen-out reachable; all callbacks mapped.
```
A failed gate means the survey isn't ready to launch. For a live feel, run `survey-agent demo` and
click through the paths while the mock receiver (on `:9000`) prints each lifecycle event.

## 4. Human review

The `.md` and `.survey.json` are committed; a teammate reviews the PR diff. Approval here is the
gate: nothing is fielded until a person signs off.

## 5. Launch: `/launch-survey`

```
/launch-survey streaming-usage
```
Marks the survey live (pinned to an immutable version) and returns the drop-in URL to put in your
panel, CRM, or email tool:
```
https://survey.example.com/s/streaming-usage?uid={uid}&rid={rid}&group={group}
```
**`live` opens the gate; publishing is a separate step.** A production runtime serves respondents
only when a survey is `live` (a pre-live survey shows a "not open yet" page; operators preview it with
a token), and marking live pins the launched version. With the `file` backend the survey must also be
*deployed* to the runtime (`git push`, or `survey-agent launch --deploy` to run your configured deploy
hook); with a runtime definition store, `publish` makes it reachable and open in one store write. If
your deployment sets `SURVEY_LAUNCH_APPROVAL_TOKEN`, marking live needs that token (the MCP connector
always needs it). survey-agent doesn't recruit anyone: your own system sends people to the URL, and
the configured integration tells it how each response ended.

## 6. Monitor: `/survey-report` (or `dashboard --live`)

```
/survey-report  streaming-usage
```
Completes, incidence, LOI, screen-outs, over-quota, quota burndown, and question-level distributions,
rendered as an HTML report (`survey-agent report <file> --open`, with `--insights` for an AI
read-out); `survey-agent export` gives you a CSV for your own cross-tabs. For an at-a-glance view of
**every** live survey's drop-in URL plus live completes/incidence/quota in one page, run
`survey-agent dashboard --live`.

## 7. Collect answers inline: `/inline-survey`

When an agent needs clean, validated answers from a person in the middle of a task, it can run a
survey as a conversation with the `begin_survey` / `answer_survey` MCP tools. The engine still
enforces the logic and validation; the result comes back in the standard export shape (with status
`"Complete"` on a completion). Conversation mode suits single-select, multi-select, number, and text
questions; grids, rankings, and file uploads need the form.

---

### Doing it without skills (the raw commands)
```bash
survey-agent validate surveys/streaming-usage.survey.json
survey-agent compile surveys/streaming-usage.md
survey-agent simulate surveys/streaming-usage.survey.json --n 500
survey-agent demo
```

### Tip: the MCP server
Install the `[mcp]` extra and register the `survey-agent-mcp` server so Claude calls
`validate_survey`, `simulate_survey`, `launch_survey`, `survey_report` (and the rest) as native tools
instead of shelling out. See [AGENTIC_MODES.md](AGENTIC_MODES.md) and [AGENT_HOSTS.md](AGENT_HOSTS.md).
