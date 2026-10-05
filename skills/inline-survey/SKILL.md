---
name: inline-survey
description: Collect clean, logic-gated answers from a human mid-task with the begin_survey / answer_survey MCP tools — the structured-elicitation primitive. Triggers on "collect structured data", "ask the user a set of questions", "run a screener inline", "gather these fields from a person", or needing rigorous, validated answers from a human during an agent task.
---

# inline-survey: the structured-elicitation primitive

Use this when an agent needs to **ask a human a structured set of questions mid-task** and get back
clean, validated, logic-gated answers instead of free-form chat. Rather than improvising questions and
parsing prose, run a real survey-agent survey inline through two MCP tools. The engine enforces every
branch, required answer, quota and screen-out, and hands back the canonical response record: the
answers (with resolved labels), derived fields, a status and any screen-out reason code.

This needs the MCP connector (`survey-agent-mcp`), because it's a live tool loop rather than a file
workflow. See [docs/PRIMITIVE.md](../../docs/PRIMITIVE.md).

## When to use it vs. the other skills

- **inline-survey (this):** you're mid-task and need rigorous answers *from a person right now*:
  onboarding fields, an eligibility screen, a structured intake, a preference set. You relay
  messages; the engine owns correctness.
- **author-survey / edit-survey / add-logic / test-survey / launch-survey / survey-report:** you're
  *building, fielding or reporting on* a survey for respondents who take it at its `survey_url`.
  Different job.

## The loop

1. **`begin_survey`**: start a session. Pass **either** `survey` (a survey id from `survey://catalog`,
   or a path to a `.survey.json`) **or** `survey_json` (an inline spec), plus optional
   `url_variables` (e.g. `{"uid": "u1"}`). Returns `{session_token, message, question_id, done}`.
   The first `message` includes a short notice that replies are read by an AI.
2. **Show `message` to the person** exactly as given, and collect their reply in their own words.
3. **`answer_survey(session_token, reply)`**: send the reply text. The engine maps it to a valid
   answer and decides what comes next. While the survey is still going you get
   `{message, done: false, question_id}`. Relay the next `message`.
4. Repeat 2–3 until `done: true`. The final call returns `{message, done: true, status, result}` and
   closes the session:
   - `status` is the terminal: `completed`, `disqualified` or `overquota`.
   - `result` is the canonical response record (the same shape `export` produces):
     `{id, date_started, date_submitted, status, reason_code, url_variables, survey_data, derived}`.
     `result.status` is a status word: `"Complete"` for a completion, `"Disqualified"` for a screen-out
     or over-quota. `survey_data` is keyed by question id: `{qid: {id, question, answer}}`.

## Rules that make it rigorous

- **Never invent, reword or re-order questions.** Relay the engine's `message`. A question that
  depends on an earlier answer only arrives after that answer, so don't pre-empt it.
- **Relay replies verbatim.** Don't coerce or "clean" them; the engine maps the reply to a valid
  answer. If it couldn't read a required answer, the next `message` re-asks the question. Pass that
  back to the person.
- **Respect the terminal.** On `disqualified` / `overquota`, stop and report the outcome (and
  `result.reason_code`). Don't try to get the person "past" a screen-out.
- **Display-only content** (`instructions`, `video`, `image`) arrives inside a `message`. It's shown,
  never answered.

## Minimal shape

```text
begin_survey(survey="team-intake", url_variables={"uid": "u1"})
    -> {session_token: "sv_…", message: "…What's your role?", question_id: "q_role", done: false}
answer_survey("sv_…", "i lead ux research")
    -> {message: "How big is your research team?", done: false, question_id: "q_team"}
answer_survey("sv_…", "about 6 of us")
    -> {message: "Thanks — all done!", done: true, status: "completed",
        result: {status: "Complete", reason_code: null,
                 survey_data: {q_role: {id, question, answer: "Researcher"}, q_team: {…}},
                 derived: {…}, url_variables: {uid: "u1"}, …}}
```

## What works in a chat

Conversation mode suits **single-select, multi-select, number and text** questions. **Grids,
rankings and file uploads are not supported in chat.** There is no automatic fallback to a form, so
keep those out of a survey you plan to run inline (or send the person the survey's `survey_url`
instead). Replies are read by an LLM, so for natural wording the connector needs a model key
(`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`); without one an offline stub runs and reads replies
mechanically.

Sessions live in the connector process's memory until `done`, so `begin_survey` and its
`answer_survey` calls must reach the same process (see the operational note in
[docs/PRIMITIVE.md](../../docs/PRIMITIVE.md#operational-note-sessions)).

## Authoring the survey it runs

The inline survey is an ordinary survey-agent spec. Author, validate and simulate it with the normal
loop (`author-survey`, `test-survey`) so the logic is proven before you run it inline. Keep it short.
