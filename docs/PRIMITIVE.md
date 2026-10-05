# survey-agent as the structured-elicitation primitive for agents

> A typed, logic-gated, auditable way for an agent to ask a human something.

## The gap

Agents are getting good at calling APIs and tools. They're still bad at **asking a human something
rigorously.** When an agent needs eligibility, preferences, consent, or structured intake from a
person mid-task, it ad-libs free-form questions and parses the prose back. There's no branching it can trust,
no quotas, no screen-out, no validation, and no record of what was asked. That's fine for chit-chat and
terrible for anything you'll act on.

survey-agent fills that gap. A survey is **logic as data**, run by a tiny fixed engine, so an agent
can hand a person a real, branching, validated instrument and get back **clean structured data**
without the agent (or the model) being trusted to enforce the logic.

## The primitive: `begin_survey` → `answer_survey`

Two MCP tools (also a plain-Python class, see below) turn any survey into something an agent runs
inline:

```text
begin_survey(survey="" | survey_json={...}, url_variables={})
    -> { session_token, message, question_id, done }
answer_survey(session_token, reply)
    -> { message, done: false, question_id }                  # …while going
    -> { message, done: true, status, result }                # …when finished
```

`survey` is a survey id from the deployment's catalog or a path to a `.survey.json`; `survey_json`
passes a spec inline instead. When finished, `status` is the engine's terminal status (`completed`,
`disqualified`, `overquota`) and `result` is the canonical response record.

The calling agent's job is only to **relay messages**: show `message` to the person, send their reply
back. The **engine** decides what's next (the next eligible question, a skip, a screen-out, an
over-quota terminal) and validates every answer. The agent never decides eligibility, so it can't get
the logic wrong.

When it's done, `result` is the canonical record (the same export shape the web runtime stores):

```jsonc
{
  "id": "…",                            // opaque, system-generated response id
  "status": "Complete",                 // or Disqualified / Partial
  "reason_code": null,                  // e.g. "non_user" on a screen-out
  "survey_data": {
    "q_role":  { "id": "q_role",  "question": "What's your role?", "answer": "Researcher" },
    "q_team":  { "id": "q_team",  "question": "How big is your team?", "answer": "2–10" }
  },
  "derived":  { "segment": "smb" },     // hidden/calc fields
  "url_variables": { "uid": "…" }
}
```

Logic-validated answers with the resolved option labels, not raw free text you have to re-parse.

**Question types in chat:** single choice, multiple choice, numbers and text work well. Grids,
rankings and file uploads aren't supported in conversation mode, and there's no fallback to a form,
so keep surveys you run through this primitive to the types that work in chat.

## In one chat (what the agent sees)

```text
agent → begin_survey("team-intake")
     ← "To set this up, a couple of quick questions. What's your role?"   (token sv_…)
agent → answer_survey(sv_…, "i lead ux research")
     ← "Got it. How big is your research team?"
agent → answer_survey(sv_…, "about 6 of us")
     ← done · status completed · result.status "Complete" · result.survey_data = {q_role: "Researcher", q_team: "2–10"}, derived.segment = "smb"
```

The agent now has typed `survey_data` to branch its own behavior on, and a record of exactly what was
asked and answered.

## Or directly in Python (no MCP)

```python
from survey_agent.conversation import Conversation
from survey_agent.engine import export

convo = Conversation(spec, url_vars={"uid": "u1", "rid": "1"})
print(convo.opening()["message"])
while not (out := convo.reply(user_text)).get("done"):
    user_text = ask_the_human(out["message"])
record = export.response_from_session(convo.rt, convo.session)   # the same canonical result
```

Same engine, same guarantees, just without the MCP transport. Drop it into an Agent-SDK / LangChain
tool, a voice loop, or your own app.

## Why this is safe to give an agent

- **The engine is authoritative, not the model.** Branching, quotas, screen-out, and validation are a
  bounded AST (`engine/ast_eval.py`): no `eval`, no scripting. The LLM only phrases a question and maps
  a free-text reply to a *valid* answer; the engine validates and decides flow.
- **Specs are checked before they run.** A survey passes the JSON Schema and the validator before
  it can be used; run `simulate` on it too before handing it to an agent.
- **Sending replies to an LLM is disclosed and recorded.** Conversation mode always uses the
  configured LLM to read replies, so the opening message discloses it; any adaptive follow-ups are
  recorded as `probes`.
- **You own it.** Self-hosted, with the model you choose. The only outside service in the loop is
  the LLM provider you configure (none at all with the offline stub).

## Operational note: sessions

A `begin_survey` session lives in the **connector process's** memory until `answer_survey` returns
`done`. Two consequences worth knowing:

- **Same worker.** `begin_survey` and its follow-up `answer_survey` calls must reach the same process.
  A local stdio connector is always fine. A multi-worker remote deployment (OAuth in front,
  `--transport streamable-http`) must pin a session to its worker — sticky routing — or run a single
  worker, until a shared session store is wired in.
- **Bounded.** Abandoned sessions (an agent that begins and never finishes) don't pile up: the store is
  capped and evicts the least-recently-used session, so an active conversation always survives while a
  forgotten one is reclaimed. Tune the cap with `SURVEY_MCP_MAX_SESSIONS` (default 256).

## What it's good for

Eligibility or screening before an agent does expensive work, preference and intake elicitation,
consent capture, onboarding, human-in-the-loop approvals that need *structured* answers, and routing a
qualified person into the next step. Anywhere an agent would otherwise free-text its way through
questions and hope the parse holds.

## Get it

Add the MCP connector (setup per host in [AGENT_HOSTS.md](AGENT_HOSTS.md)), or
`pip install "survey-agent[mcp]"` and run `survey-agent-mcp`. The PyPI package ships with the first
release; until then install from GitHub:
`pip install "survey-agent[mcp] @ git+https://github.com/paulneto/survey-agent"`. More agentic surfaces (authoring,
critique, persona pre-test, conversation, auto-insight): [AGENTIC_MODES.md](AGENTIC_MODES.md).
