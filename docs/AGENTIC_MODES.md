# Agentic modes

survey-agent is built for an AI to do the work. Because **logic is data and the engine is a pure
interpreter** (see [LOGIC_IS_DATA.md](LOGIC_IS_DATA.md)), an LLM can plug in at several points
without touching the part that decides flow, screen-outs and quotas. This page covers the agentic
surfaces that are built today, and some directions they could go next.

Two rules apply to anything that sends *respondent* data to an LLM (see
[DATA_SECURITY.md](DATA_SECURITY.md)): it is opt-in or disclosed to the respondent, and the
runtime's LLM calls on answers are recorded on the response (`llm_audit`).

The LLM is whichever you configure: `ANTHROPIC_API_KEY` (the `[llm]` extra) or `OPENAI_API_KEY`
(the `[openai]` extra). With no key, each surface falls back to a deterministic offline stub, so
everything below runs in tests and demos without a network.

---

## Built

### 1. Agentic authoring (skills + MCP)

Natural-language brief → validated survey JSON → simulate → human approval → launch → report. The
authoring model only ever sees survey *definitions*, never responses. See the `skills/` directory,
`src/survey_agent/mcp/server.py` and [SKILL_WALKTHROUGH.md](SKILL_WALKTHROUGH.md).

### 2. Contextual enrichment

Declare a per-respondent fact in `settings.enrichments` (for example `event_type` + `window` +
`criteria` + `operation`). It is resolved for the respondent and can be used in logic, piping
(`{{ctx:…}}`) and routing.

The provider is a seam, selected with `SURVEY_ENRICH`:

- `static` (the default): fixtures, for demos and tests;
- `firebolt`: a reference warehouse adapter, where SQL identifiers are validated and values are
  parameterized;
- your own: an import path, `your.pkg:Provider`.

Anything else, such as an HTTP API, another database, geo-IP, user agent or a respondent's earlier
answers in another survey, means writing an adapter. None of those are built in. See `enrich/`.

### 3. LLM analysis of answers (`source: llm`)

An enrichment with `source: llm` sends declared answer fields to the LLM to derive a value: the
sentiment or theme of an open end, or an author-written prompt such as a one-line summary of the
respondent.

- It runs **at completion** and the value is **stored on the response** (exported under `derived`,
  with the call recorded in `llm_audit`).
- It does **not** drive live logic, piping or routing, and it is **never shown to the respondent**.
  Use it for analysis and for your downstream systems.
- It is off unless the deployment enables it (`SURVEY_LLM_ENRICH=1`) **and** the spec sets
  `opt_in: true`. A spec can also require a consent question (`requires_consent`). Disclose it in
  your consent text.
- If the model call fails, the value is left empty and the survey completes normally.

Code: `enrich/__init__.py`, `ai.py`.

### 4. Hidden values and calculations

A `hidden` block computes scores, segments and other derived fields with a bounded **calc tree**
(data, not code). Values are stored on the response and usable in logic, piping and quotas,
including a **quota on a computed segment**. Code: `engine/calc.py`.

### 5. Conversational mode

An AI **administers the survey as a chat**, using the **same survey JSON**. The engine stays the
source of truth for flow, branching, screen-outs, quotas and validation. The LLM only (a) **phrases**
each question and (b) **maps the free-text reply to a valid answer**, which the engine then validates
before advancing. Run it with `survey-agent convo`, at `/chat` on the runtime, or over MCP (§6).
Code: `conversation.py`.

```
engine: "next eligible question + valid answers"  ──▶  LLM phrases it  ──▶  respondent replies freely
   ▲                                                                              │
   └──  engine validates + advances (logic/quota/screen-out)  ◀── LLM maps reply to a valid answer
```

**What works in chat:** single choice, multiple choice, numbers and text. **What doesn't:** grids,
rankings and file uploads aren't supported in conversation mode, and there is no automatic fallback
to a form. Field surveys that use them as forms. Without an LLM key the offline stub runs the flow
correctly but reads mechanically.

Try it offline on the bundled example:

```bash
survey-agent convo --scripted yes 34 "Currently subscribe" Netflix "Checking whether the price went up" Netflix "Strongly agree"
# ... ends with [survey terminal: completed]
survey-agent convo --scripted no
# ... ends with [survey terminal: disqualified]
```

(With a real key the model may add a follow-up probe turn, so a fixed script is for the offline stub.)

### 6. Survey as a tool for agents

The conversational runner is exposed over MCP as **`begin_survey` / `answer_survey`**, so any agent
can run a logic-gated survey **inline, mid-task** to collect structured data from a person, and get
back the canonical response record (`survey_data`, `derived`, status, screen-out reason). The engine
enforces branching, screen-outs, quotas and validation; the calling agent only relays messages and
never decides eligibility. See [PRIMITIVE.md](PRIMITIVE.md).

### 7. Adaptive probing

In conversational mode, a thin open-text answer can trigger **one** follow-up question from the LLM
to draw out detail, within a per-session budget. It's opt-in per question (`probe: true`) or for the
whole survey (`settings.ai.probe_open_text`). It never changes flow or the recorded answer: the
original answer stands, and the follow-up and its reply are stored as a derived `probes` field.

### 8. Auto-insight report

After fielding, an LLM analyst turns the aggregated results plus a sample of open ends into a
read-out: headline, key findings, open-end themes, data-quality watch-outs and suggested follow-up
questions. It's embedded in the HTML report (`report --insights`) or printed (`insights`). Opt-in,
with a deterministic stub when there's no key. Code: `insights.py`, `report.py`.

### 9. Persona pre-test

`survey-agent pretest` drives synthetic respondents through the survey to preview incidence and who
screens out before you field. Declare the **population** to test against with `--audience <file>`
(weighted segments of traits) or `--trait k=v`. Answers are made *coherent* with each persona's
traits (a `usage: never` persona reliably hits the screen-out), and the engine still validates
everything. Personas come from a seam (`SURVEY_PERSONAS`): the core ships `synthetic`, and you can
point it at an import path for personas grounded in your own data. Format and plugin contract:
[PERSONAS.md](PERSONAS.md). This is for **testing only**, not a substitute for real respondents.

### 10. Methodology critic

`survey-agent critique` (and the `critique_survey` MCP tool) reviews a draft for what a schema can't
catch: leading or double-barreled questions, biased scales, missing opt-outs, overlapping ranges and
jargon. It returns severity-ranked issues with suggested fixes. It is advisory, not a pass/fail gate,
and it sees only the definition. Offline, it falls back to heuristics. Code: `ai.py`.

---

## Directions

Ideas that fit the same pattern: the engine stays authoritative and the LLM assists. None of these
are built.

- **On-the-fly translation:** an LLM phrases questions and reads replies in the respondent's `lang`,
  and answers normalize back to canonical option values. The engine never sees language at all.
- **Open-end quality scoring:** an LLM judges open ends for gibberish, effort and duplication and
  attaches a quality score to the completion passback, so the receiving system can decide what to do
  with it.
- **Respondent help:** a "what does this mean?" affordance where the respondent can ask the AI to
  clarify a question, read-only, never changing the survey logic.
- **Agent-assisted extensions:** someone extending survey-agent describes a new question type, store
  or enrichment source to an agent, which scaffolds it and its tests against the extension points in
  [ENGINEERING.md](ENGINEERING.md).

The throughline: the engine is the trustworthy, testable core, and every agentic mode is a front end
or a context source plugged into it. That's what makes them safe to add, and why a survey authored
once can be delivered as a form, a chat or an agent tool without changing its logic.
