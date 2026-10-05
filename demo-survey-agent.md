# survey-agent: try-it walkthrough

A guided tour of the whole survey lifecycle for someone who has just installed survey-agent. Each
step is a plain-language prompt you type to your agent: Claude Code in a clone of this repo (which
loads the bundled skills and runs the `survey-agent` CLI), or a desktop/chat agent with the
survey-agent MCP connector. The agent drafts, tests, fixes, launches and reports by talking. The walk
follows one story, an "AI tools at work" screener.

> **Before you start.** Install survey-agent as described in the [README](README.md) and run
> `survey-agent doctor` to confirm the install. Authoring (step 3) and the AI read-out (step 13) need
> a model key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`); everything else runs offline.
> Run the prompts from one working directory: local responses are written to `./survey_data`.

---

## Quick smoke test (no model key needed)

Just checking the install works end to end? Run these against a bundled example survey, all offline:

1. **"Run doctor and confirm my install and config are healthy."**
   → `survey-agent doctor`: the install, the resolved store / integration / definition source, and the
   public base URL.
2. **"List the example surveys, pick one, validate it, and simulate 200 respondents to confirm the
   coverage gate passes."**
   → `list`, `validate`, `simulate --n 200`.
3. **"Start the local demo with that survey and give me the link so I can take it."**
   → `survey-agent demo --survey surveys/<slug>.survey.json`: the runtime on :8000 and a mock receiver
   on :9000 that prints each lifecycle event. Take the survey once in your browser.
4. **"Export the responses for that survey and show me the one I just submitted."**
   → `export`: confirms the response was captured in the local store.
5. **"Generate an HTML report for it (simulate 200 respondents if there isn't enough real data)."**
   → `report --open` (or `report --simulate 200 --open`).
6. **"Set it to paused, then back to tested."**
   → `status --set paused` / `status --set tested`: the lifecycle toggles.

If those six do something sensible, survey-agent is wired up. The full walkthrough below adds
authoring, logic, launch, passback and backups.

---

## Full walkthrough

### 0 · Sanity check

1. **"Run doctor and tell me which store, integration and definition source I'm on."**
   → surfaces `SURVEY_STORE` (default `file`), `SURVEY_INTEGRATION` (default `none`) and
   `SURVEY_DEFINITION_SOURCE` (default `file`), and flags the multi-clone editable-install trap.
2. **"List the example surveys and what each one is for."**
   → `list`: the registry with each survey's description and lifecycle state.

### 1 · Author

3. **"Draft a screener for a study on AI tools at work: do you use AI assistants for work, which
   ones, hours saved per week, and your role. Screen out anyone who doesn't use them. Aim for about
   3 minutes and about 40% incidence. Save it."**
   → the agent writes the Survey JSON plus a readable `.md` into `surveys/` (skill: `author-survey`;
   CLI: `survey-agent author`).

### 2 · Check

4. **"Validate it, then simulate 500 respondents. Show me incidence, the screen-out reasons, the
   estimated length, and any path that never gets exercised."**
   → `validate` + `simulate --n 500`, the coverage gate. The agent fixes anything unreachable
   (skill: `test-survey`).
5. **"Critique the wording: flag leading or double-barreled questions and any missing 'prefer not to
   say', and fix the ones you agree with."**
   → `critique` and an edit pass (skill: `edit-survey`).

### 3 · Shape the logic

6. **"Add a quota: stop at 300 qualified completes."**
   → a quota with an over-quota terminal (skill: `add-logic`).
7. **"Add a follow-up that only shows to people who use 3 or more AI tools, asking which one they'd
   miss most."**
   → a branch on an earlier answer, placed on a later page.
   Then: **"For each AI tool they picked, ask how often they use it for work."**
   → a page `repeat` over the tools question: the page runs once per tool, `{{loop:label}}` names it,
   and each answer is stored and exported per tool (`q_freq[chatgpt]`).
8. **"Add an attention check halfway through and screen out anyone who fails it."**
   → an ordinary question plus a `disqualify` rule with its own reason code.

After each change, ask **"Re-validate and re-simulate."** to keep the coverage gate green.

### 4 · Preview it

9. **"Start the local demo with this survey and give me the link so I can take it myself."**
   → `survey-agent demo --survey surveys/<slug>.survey.json`. Open the link and use the browser's
   device toolbar to check desktop **and** mobile.
10. **"Walk a few synthetic personas through it and show me who qualifies and who screens out."**
    → `pretest --n 6`: a realistic incidence preview before fielding (needs a model key for
    role-played answers).

### 5 · Launch

11. **"Launch it and give me the drop-in survey URL."**
    → `launch` (skill: `launch-survey`). You always get the URL,
    `<base>/s/<slug>?uid={uid}&rid={rid}&group={group}`, plus the operator checklist. Marking it
    **live** is gated:
    - If `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, the agent asks **you** for the token and for
      confirmation of the checklist (`launch --approve <token> --ack-responsibilities`). It can't mark
      the survey live without them.
    - If it isn't set, a local CLI run can mark it live, but over the MCP connector marking live
      always needs the token.

    The base URL comes from `SURVEY_PUBLIC_BASE_URL`, or is derived from the host your runtime runs on;
    a `localhost` base reaches nobody, and `launch` warns you. Marking live doesn't deploy: with the
    default `file` definition source, the agent tells you to run your deploy step (or
    `launch --deploy` with a configured `deploy.command`). With a runtime definition store
    (`SURVEY_DEFINITION_SOURCE=mongo` or `http`), `publish` updates the hosted runtime directly.
12. **"Configure the passback: on complete, redirect respondents to my panel's complete URL with uid
    and rid appended; send screen-outs to its terminate URL."**
    → the `redirect` integration: `SURVEY_INTEGRATION=redirect` with `SURVEY_REDIRECT_COMPLETE_URL`,
    `SURVEY_REDIRECT_TERMINATED_URL` and `SURVEY_REDIRECT_PASSBACK=uid,rid`. The browser is redirected
    on every terminal with those values appended. Use your panel's own variable names in
    `settings.url_variables` if they differ from `uid` / `rid`.

### 6 · Operate and report

13. **"How's it doing: completes, incidence, median length, screen-outs and over-quota?"** then
    **"Export the responses and cross-tab hours saved by role, and give me an AI read-out of the open
    text."**
    → `report`, `export` (the cross-tab is computed from the exported rows) and `insights`
    (skill: `survey-report`). With no real responses yet, add "simulate 300" to any of these.
14. **"Pause it for now."** … later: **"Set it live again."**
    → `status --set paused`, then `status --set live` (which goes through the same launch gate as
    step 11).

### 7 · Safety net

15. **"Export every survey version to disk as a backup, then check whether anything in the
    definition store has drifted from the files."**
    → `defs-export --all` + `defs-diff` (`defs-import` rebuilds a store from the files). For
    responses, `survey-agent backup` writes a backup of the store.

---

**What to notice:** the agent **tests its own work** (the validate and simulate loop in steps 4–8),
the **launch gate** keeps a human in charge of fielding (step 11), and everything is files plus plain
language.

The seven bundled skills are `author-survey`, `edit-survey`, `add-logic`, `test-survey`,
`launch-survey`, `survey-report` and `inline-survey` (for collecting structured answers from a person
in the middle of an agent task; see [docs/PRIMITIVE.md](docs/PRIMITIVE.md)).
