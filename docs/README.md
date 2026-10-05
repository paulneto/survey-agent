# survey-agent documentation — map

Start with what you're trying to do. Each doc has one job, and where a doc owns a topic it says so, so a
fact lives in one place and the other docs link to it rather than restating it. `survey-agent docs` prints
this map from the command line (local files in a checkout, GitHub URLs from a pip install).

## By task

| If you want to… | Read | Who it's for |
|---|---|---|
| Understand what this is and why it's shaped this way | [../README.md](../README.md) | everyone |
| Drive survey-agent as an LLM / agent | [../AGENTS.md](../AGENTS.md) | agents and the people wiring them up |
| Try it end-to-end on the CLI in about ten minutes | [QUICKSTART.md](QUICKSTART.md) | a developer kicking the tires |
| Operate surveys day to day by talking to an agent (author → launch → report) | [USER_GUIDE.md](USER_GUIDE.md) | researchers and survey operators |
| Build and test a first survey with the skills, step by step | [SKILL_WALKTHROUGH.md](SKILL_WALKTHROUGH.md) | first-time skill users (USER_GUIDE is the full reference) |
| Stand up a shared team deployment | [DEPLOY.md](DEPLOY.md) | whoever hosts it for a team |
| Pick a host and understand the local-control-plane + hosted-runtime shape | [HOSTING.md](HOSTING.md) | whoever chooses infrastructure |
| Use survey-agent as an agent tool (`begin_survey` / `answer_survey`) | [PRIMITIVE.md](PRIMITIVE.md) | agent builders |
| See exactly what is supported, partly supported, or out of scope | [FEATURE_COVERAGE.md](FEATURE_COVERAGE.md) | evaluators |
| Understand the agentic surfaces and conversational delivery | [AGENTIC_MODES.md](AGENTIC_MODES.md) | everyone |
| Run pretests against a declared population (persona/audience format) | [PERSONAS.md](PERSONAS.md) | whoever runs pretests |
| Use the local operator console | [CONSOLE.md](CONSOLE.md) | survey operators |
| Understand the architecture and the swappable seams | [ENGINEERING.md](ENGINEERING.md) | engineers |
| Understand why logic is data, not code, plus the full logic reference | [LOGIC_IS_DATA.md](LOGIC_IS_DATA.md) | evaluators, engineers, agents, anyone authoring logic |
| Work out which agent host (Claude Code / Cowork / ChatGPT / Codex) can do what | [AGENT_HOSTS.md](AGENT_HOSTS.md) | anyone picking a surface |

## Security and responsibilities

| Doc | Job |
|---|---|
| [OPERATOR_RESPONSIBILITIES.md](OPERATOR_RESPONSIBILITIES.md) | **Owns** the short pre-fielding checklist (store, erasure, consent, URL integrity, transport). Printed at launch. |
| [DATA_SECURITY.md](DATA_SECURITY.md) | Deeper data-handling guidance: what the engine does, and what stays the deployer's responsibility. |
| [../SECURITY.md](../SECURITY.md) | How to report a vulnerability. |

## Reference and contributor

| Doc | Job |
|---|---|
| [DEPLOY.md → Appendix C](DEPLOY.md#appendix-c--command-cheat-sheet) | **Owns** the command cheat sheet (also `survey-agent help`). |
| [dev/DESIGN_DECISIONS.md](dev/DESIGN_DECISIONS.md) | **Owns** the settled design decisions and their rationale. |
| [dev/TEMPLATES.md](dev/TEMPLATES.md) | The template / scaffolding seam. |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) · [../CHANGELOG.md](../CHANGELOG.md) · [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | Standard repo files. |

## Single source of truth

When two docs would otherwise explain the same thing, the owner below states it and the rest link to it:

- **Commands and flags** → `survey-agent help` and the [cheat sheet](DEPLOY.md#appendix-c--command-cheat-sheet).
- **Deploy and publish**: marking a survey *live* is lifecycle bookkeeping; *publishing* it to a hosted runtime is a separate step → [DEPLOY.md](DEPLOY.md).
- **Host choices, storage rungs and the Render blueprints** → [HOSTING.md](HOSTING.md) and [DEPLOY.md Appendix A](DEPLOY.md#appendix-a--one-time-setup-the-service).
- **Authoring from the Claude desktop app (Cowork / Code)** → [DEPLOY.md Appendix D](DEPLOY.md#appendix-d--authoring-from-claude-desktop-cowork--code).
- **Pre-fielding responsibilities** → [OPERATOR_RESPONSIBILITIES.md](OPERATOR_RESPONSIBILITIES.md).
- **Lifecycle states and which ones gate serving** (`live` is served; `paused`/`closed` stop fielding) → [DEPLOY.md](DEPLOY.md) and `survey-agent status`.
- **What the engine supports** (question types, validation, logic, repeating, quotas, retake, pause) → [FEATURE_COVERAGE.md](FEATURE_COVERAGE.md).
- **Why a decision is the way it is** → [dev/DESIGN_DECISIONS.md](dev/DESIGN_DECISIONS.md).
