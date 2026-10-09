# Which agent can drive survey-agent, and how

Claude Code, Claude Desktop, claude.ai, ChatGPT, Codex — these are **not interchangeable** here, and the
differences are not about model quality. They're about what the *host* can do: whether it can touch
files, run a shell, reach the network, and speak MCP.

This page maps each host to what it can actually do, and what it can't.

> **One thing is true of every host:** an agent can author, validate, simulate, critique and preview a
> survey on its own, but fielding it to real people needs things only an operator can supply: a
> public runtime, an audience, and (when configured) a launch approval. See §6.

---

## 1. The capability matrix

| Host | Files | Shell + net | MCP | How it drives survey-agent | Author | Validate / Simulate | Local preview |
|---|---|---|---|---|---|---|---|
| **Claude Code** (CLI / VS Code) | ✅ | ✅ | ✅ | the CLI + bundled skills — **best surface** | ✅ | ✅ | ✅ |
| **Claude Desktop — code** | ✅ | ✅ | ✅ | same as Claude Code | ✅ | ✅ | ✅ |
| **Claude Desktop — cowork / chat** | ✅ | ❌ no terminal | ✅ | MCP connector (stdio) | ✅ | ✅ | operator runs `serve` |
| **claude.ai** (web) | ❌ | ❌ | remote only | needs a *hosted* connector you stand up | ⚠️ | ⚠️ | ❌ |
| **ChatGPT** (app) | ❌ | ❌ no egress | connector | needs a *hosted* connector you stand up | ⚠️ | ⚠️ | ❌ |
| **Codex CLI** | ✅ | ⚠️ config-dependent | ✅ | the CLI + `AGENTS.md` | ✅ | ✅ | ✅ |
| **Codex cloud / sandboxed** | ✅ | ⚠️ restricted | ✅ | CLI, deps pre-installed | ✅ | ✅ | ❌ |

⚠️ = possible, but only after an operator hosts an endpoint (§4). Host capabilities change; treat the
Files / Shell / MCP columns as the thing to check, not the product name.

## 2. What the four capabilities are actually for

| Capability | What it unlocks |
|---|---|
| **Read/write files** in a working directory | authoring `surveys/<slug>.survey.json`, auto-loading `skills/`, `save_survey` persisting to disk |
| **Shell + outbound network** | `pip install survey-agent` / `uvx` — i.e. getting the package at all |
| **MCP client** | the tools-only path: 22 typed tools, resources (`survey://schema`, `survey://catalog`, themes, templates, and one per survey), 2 prompts |
| **Long-lived local process** | `survey-agent serve` to click through the survey before launch |

A host missing *files* can still drive survey-agent over MCP — because the **MCP server** runs locally
and has the file access the chat surface lacks. That's the whole trick behind the no-terminal path.

---

## 3. Claude Code · Claude Desktop *code* · Codex CLI — the full-capability hosts

These have files, a shell, and (usually) network. Nothing is withheld from them.

```bash
pip install "survey-agent[llm]"      # Claude, GPT or any OpenAI-compatible model
survey-agent selftest                # zero config, no key, no network
survey-agent init                    # scaffold surveys/ + config + .env
```

Then the whole loop, either by running commands or by asking in plain language:

```bash
survey-agent author "screen US adults who stream paid video, quota 300"
survey-agent validate surveys/<slug>.survey.json --json
survey-agent simulate surveys/<slug>.survey.json --n 500     # the coverage gate
survey-agent critique surveys/<slug>.survey.json
survey-agent serve --multi --dir surveys                      # click through it
survey-agent report   surveys/<slug>.survey.json --open
```

**How the agent orients itself here:**

- **Claude Code** reads `CLAUDE.md` automatically, and `.claude/skills` (a symlink to `skills/`)
  auto-registers the 7 skills (`author-survey`, `edit-survey`, `add-logic`, `test-survey`,
  `launch-survey`, `survey-report`, `inline-survey`), so *"build me a screener for X"* triggers
  `author-survey` with no setup.
- **Codex CLI** reads **`AGENTS.md`**, which is written for exactly this: the loop, the CLI table, the
  self-correction signals, and the authoring rules.
- Both should read **[LOGIC_IS_DATA.md](LOGIC_IS_DATA.md)** before writing any conditional logic.

**Codex-specific:** outbound network is often restricted or off by default in sandboxed/cloud Codex.
If `pip install` fails, that's the sandbox, not survey-agent — pre-install the dependency (it's just
`jsonschema` + `PyYAML` + `regex`) or enable egress. `validate`, `simulate` and `critique` need **no**
network and **no** key once installed, so a fully offline sandbox still runs the entire correctness
loop.

---

## 4. Tools-only hosts: Claude Desktop cowork/chat, claude.ai, ChatGPT

These can't run a shell, so they drive survey-agent through **MCP**. Two very different setups:

### 4a. Local connector (stdio) — Claude Desktop cowork / chat

The connector runs on the operator's own machine, so it has file access even though the chat surface
doesn't. `save_survey` writes a real file into their working directory.

```bash
claude mcp add survey-agent -- uvx --from "survey-agent[mcp,llm]" survey-agent-mcp
```

For Claude Desktop, register it under `mcpServers` (template: [`.mcp.json.example`](../.mcp.json.example));
full walkthrough in **[DEPLOY.md Appendix D](DEPLOY.md#appendix-d--authoring-from-claude-desktop-cowork--code)**.

The agent needs no documentation — the tools describe themselves:

- `capabilities()` — *"Call this first so you don't assume a store/source/integration that isn't installed."*
- `survey://schema` — the authoritative grammar, as a resource
- the `author_survey` prompt — the full loop, including *"a question that depends on a PRIOR answer goes
  on its own LATER page"* and *"then STOP for human review"*
- `deployment_readiness()` — is this deployment safe to field?

### 4b. Remote connector (HTTP) — claude.ai web, ChatGPT

**This works, but it is not turnkey: somebody has to host it.** The MCP server supports remote transports:

```bash
survey-agent-mcp --transport streamable-http     # or --transport sse
```

You must put **authentication in front of it** (OAuth or equivalent) — it is an authoring surface with
file and store access, not something to expose openly. survey-agent does **not** ship a hosted endpoint,
so this is an operator project, not an install step.

**ChatGPT specifically** (as of 2026-10): its code sandbox has no outbound network to PyPI, so it can't
install the package and drive the CLI. Its path is a connector pointed at an endpoint you host.

**Respondents don't take surveys inside the chat host.** The respondent runtime is a Python web
server that writes to your store, so a survey isn't served from a claude.ai Artifact or a chat
canvas. Host the runtime (see [HOSTING.md](HOSTING.md)) and share its link. To ask the person *in*
the chat a few structured questions, use `begin_survey` / `answer_survey` instead
([PRIMITIVE.md](PRIMITIVE.md)).

---

## 5. The distinction people get wrong: *driving host* vs *authoring model*

These are **orthogonal**, and the repo's `[llm]` extra is about the second one:

| | What it is | How you choose it |
|---|---|---|
| **Driving host** | the agent running the loop — Claude Code, cowork, Codex CLI… | whichever surface you work in (§1) |
| **Authoring model** | the LLM that turns a brief into survey JSON, powers conversational mode, and writes the insights read-out | `pip install "survey-agent[llm]"` + `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (plus `OPENAI_BASE_URL` for another OpenAI-compatible service) |

So all four combinations are legal — drive from Claude Code and author with a GPT key, or drive from
Codex and author with Claude. And **neither is required for correctness**: `validate`, `simulate` and
`critique` are pure-engine and run with no key at all. With no key, `author` degrades to a deterministic
offline stub and says so:

```
ℹ  No Claude/Codex key found — authoring with the OFFLINE STUB (a minimal screener).
```

---

## 6. What no agent does alone: field the survey

`launch` gives you a drop-in `survey_url` and marks the survey live **in local bookkeeping only**:

```
🟢 survey_url for 'glp1' — drop this into your panel / CRM / email link:

  http://localhost:8000/s/glp1?uid={uid}&rid={rid}

⚠️  Marked live LOCALLY only — this did NOT publish the survey. Respondents can't reach it
    until your hosted service is deployed with this file.
```

Three things stand between that and a real respondent, and each one is an operator's decision:

1. **A public runtime.** The URL only reaches people once the runtime is deployed somewhere public.
   The runtime derives its public URL from the host it runs on (set `SURVEY_PUBLIC_BASE_URL` to
   override). See [HOSTING.md](HOSTING.md).
2. **An audience.** Sampling, targeting, panel quotas and incentives are **out of scope by design**:
   survey-agent emits a URL, and your panel, CRM or email sends people to it.
3. **The launch approval.** When `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, nothing can mark a survey
   live without that token: not the CLI (`launch --approve TOKEN`, plus the operator checklist
   acknowledgement `--ack-responsibilities` or `SURVEY_OPERATOR_ACK=1`), not the console, not MCP.
   When it is unset, the local CLI and console can mark a survey live, but the MCP connector (and the
   API gateway) still can't: `launch_survey` always needs the token **and**
   `acknowledge_responsibilities=true`. Set the token on any deployment where an agent drives the CLI.

`launch` also prints the five operator responsibilities every time: response store, erasure/DSAR,
consent and disclosure, URL integrity, and transport and access. See
[OPERATOR_RESPONSIBILITIES.md](OPERATOR_RESPONSIBILITIES.md).

**In short:** *"build me a survey"* is autonomous. *"Launch it"* is an operator's call.

---

## 7. One more myth: PyPI is not discovery

Publishing to PyPI makes survey-agent **installable**, not **discoverable**. Agents don't browse PyPI.
For an agent to reach for it, one of these has to be true:

- the user names it — *"use survey-agent to build me a screener"* ← the realistic path
- a web search surfaces it (needs inbound links; a new package ranks nowhere)
- it's in the model's training data (not for a long while)
- it's registered in an MCP registry the host indexes ([`server.json`](../server.json) is ready for that)

Design your rollout around the first one.

---

## See also

- [AGENTS.md](../AGENTS.md) — the LLM entry point: the loop, the CLI, the self-correction signals
- [LOGIC_IS_DATA.md](LOGIC_IS_DATA.md) — read before authoring any conditional logic
- [DEPLOY.md Appendix D](DEPLOY.md#appendix-d--authoring-from-claude-desktop-cowork--code) — Claude Desktop connector setup
- [HOSTING.md](HOSTING.md) — what your deployment actually needs
- [PRIMITIVE.md](PRIMITIVE.md) — `begin_survey`/`answer_survey`, for any agent needing structured data from a human
