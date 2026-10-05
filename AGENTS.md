# AGENTS.md — driving survey-agent as an LLM

This file is the entry point for an LLM/agent (Claude Code, the Claude desktop app / Cowork, or any
file-access agent) that needs to **author, test, field, or operate a survey** with survey-agent. If
you're a human, [README.md](README.md) is the friendlier door; everything here is true for you too.

survey-agent is designed to be driven by an agent. A survey is a single `.survey.json` document; a
small, fixed, pure-Python engine interprets it. You (the agent) do the authoring, critique, and
results analysis; the engine validates, simulates, and runs it — so your work is always checked
against ground truth, never just "an LLM emitted some JSON."

## The loop

```
brief → draft → validate → simulate (coverage gate) → [critique / pretest] → human approval → launch → report
```

**Get a human's approval before launch.** Launching fields a survey to real people and can spend
budget. The engine enforces this with an approval token:

- When `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface can mark a survey `live` without it: not the
  CLI (`launch`, `status --set live`, `publish`; pass `--approve <token>`), not the console, and not the
  MCP connector (pass `approval_token`). The token is something a human gives you; don't go looking
  for it.
- When it is unset, the local CLI and console can mark a survey live, but the MCP connector still
  cannot.

You always get the `survey_url`; only flipping the survey to *live* is gated.

## How to drive it

Pick the surface you're on. **Hosts are not interchangeable**: Claude Code, Cowork, ChatGPT and
Codex differ in whether they have files, a shell, network, and MCP; see
**[docs/AGENT_HOSTS.md](docs/AGENT_HOSTS.md)** for the capability matrix and per-host setup.

| You are… | Use | Notes |
|---|---|---|
| Claude Code (VSCode/CLI) | the bundled [`skills/`](skills/) → the `survey-agent` CLI | full shell + file access |
| Claude desktop / Cowork / other MCP host | the **MCP connector** | typed tools over the same engine |
| Any agent that needs to ask a human something rigorously, mid-task | `begin_survey` + `answer_survey` (MCP) | the structured-elicitation primitive — see [docs/PRIMITIVE.md](docs/PRIMITIVE.md) |

**MCP connector (one line):**

```bash
claude mcp add survey-agent -- uvx --from "survey-agent[mcp,llm]" survey-agent-mcp
```

> The PyPI package ships with the first release. Until then, install from GitHub:
> `claude mcp add survey-agent -- uvx --from "survey-agent[mcp,llm] @ git+https://github.com/paulneto/survey-agent" survey-agent-mcp`
> (or `pip install "git+https://github.com/paulneto/survey-agent"` for the CLI).

Tools exposed:

- **Discover:** `capabilities`, `deployment_readiness` (is this deployment ready to field?),
  `list_surveys`, `list_templates`.
- **Author:** `save_survey`, `new_survey`, `copy_survey`, `templatize_survey`,
  `compile_markdown_survey`.
- **Check:** `validate_survey`, `critique_survey`, `simulate_survey` (the coverage gate),
  `pretest_survey`.
- **Field:** `publish_survey`, `launch_survey` and `set_survey_status` (marking live is
  approval-gated).
- **Read results:** `survey_report`, `survey_insights`, `export_survey`, `build_dashboard`.
- **Ask a human:** `begin_survey` / `answer_survey`, the inline structured-elicitation primitive (see
  the `inline-survey` skill and [docs/PRIMITIVE.md](docs/PRIMITIVE.md)).

The JSON Schema, the survey catalog, themes and templates ship as MCP resources, with
`author_survey` and `review_before_launch` prompts.

**CLI (one command per step):**

```bash
survey-agent help                         # guided overview of every command + the flow
survey-agent selftest                     # confirm the install works (validate + simulate the example)
survey-agent author "<brief>"             # run the whole loop from a plain-English brief
survey-agent validate surveys/x.survey.json
survey-agent simulate surveys/x.survey.json --n 500    # coverage gate before launch
survey-agent critique surveys/x.survey.json
survey-agent pretest  surveys/x.survey.json --n 6      # persona pre-test: incidence + length preview
survey-agent launch  surveys/x.survey.json [--approve <token>] [--ack-responsibilities]
survey-agent report  surveys/x.survey.json [--insights]
survey-agent list                         # management view
survey-agent status <id> [--set STATE]    # show or set one survey's lifecycle state
survey-agent doctor                       # resolved install + config self-diagnosis
survey-agent dsar <uid> [--delete]        # find/erase a subject's responses (DSAR)
survey-agent backup / restore <file>      # portable JSONL cold backup of responses
survey-agent outbox status|list|retry     # drain the durable webhook outbox (SURVEY_OUTBOX=1)
```

Most read/check commands accept **`--json`** for machine-parseable output (`validate`, `simulate`,
`critique`, `doctor`, `list`, `dsar`, `selftest`); `status` does not. Prefer `--json` when you're
parsing results programmatically. The exit code is non-zero when a gate fails.

## The feedback you can self-correct on

- **`validate`** returns specific, actionable errors ("unknown question ref X", "option value Y not
  declared", "merge token references an undeclared variable", "depends on 'q_x', which is on this
  page") — fix them directly. It checks merge tokens in titles, bodies, and option labels, and display
  logic that points at a question on the same or a later page (it could never show).
- **`simulate`** is the coverage gate: it runs synthetic respondents and passes only when a
  qualified respondent can reach complete and every declared screen-out is reachable ("complete +
  every declared screen-out reachable"). On failure it names the unreachable terminal or the
  screen-out that never fired, and it reports the incidence and length it measured. Use it to repair
  logic before any human sees the survey.
- **`pretest`** role-plays persona respondents to preview who actually qualifies (incidence) and how
  long it takes.
- **`critique`** flags methodology issues (double-barreled questions, leading wording, missing
  options).

## Architecture rules you must respect when authoring

- **Logic is data, never code.** Conditions are a bounded AST (`{ref, operator, value}`); operators
  are a closed set. There is no `eval`. The validator + JSON Schema are the gate.
  **[docs/LOGIC_IS_DATA.md](docs/LOGIC_IS_DATA.md)** explains why (it's what lets you self-correct
  against `validate` and `simulate`) and is the reference: the 6 `ref.kind`s, all 16
  operators, the 6 action types, the blank-answer/`equals_ordered` semantics that trip authors up,
  and what `validate` catches vs what only `simulate` catches. Read it before authoring logic.
- **Server-driven page flow.** A question that depends on another answer goes on a *later page*.
  `validate` rejects a dependency on the same or a later page.
- **Repeating is data too.** `repeat: {source, …}` on a page or question asks it once per item from an
  earlier choice question (or N times from a number). Each answer is keyed `qid[item]`
  (`q_rate[netflix]`); inside the repeat use `{{loop:label}}`, `{"kind":"loop","id":"value"}`, and
  `qid[@]` for "this item's answer". See [skills/add-logic/SKILL.md](skills/add-logic/SKILL.md).
- **Display-only types** (`image`, `video`, `instructions`) are stimulus: never required, never
  validated.
- **Conversation mode has limits.** `begin_survey` / `convo` handle choice, number and text questions;
  grids, rankings and file uploads aren't supported in conversation, so keep surveys meant for chat
  to those types.
- **Definitions are immutable + versioned.** Editing publishes a new version; in-flight sessions stay
  pinned to theirs.

See [docs/ENGINEERING.md](docs/ENGINEERING.md) for the full architecture and
[skills/author-survey/SKILL.md](skills/author-survey/SKILL.md) for the authoring playbook.

## If you're standing up a deployment

The deployer brings their own storage, integration, enrichment, and LLM. Start simple (the defaults
are the `file` store and no integration) and add complexity only when your deployment needs it. See **[docs/HOSTING.md](docs/HOSTING.md)** for
the "what does your deployment need?" decision guide and
[docs/OPERATOR_RESPONSIBILITIES.md](docs/OPERATOR_RESPONSIBILITIES.md) for what the operator owns.
Mongo, Render, Firebolt, and Mailchimp in this repo are *reference examples* of those seams, not
requirements.
