# Design decisions

The settled design decisions behind survey-agent, with the reasoning for each. Read this before
proposing a change to one of these areas: most of them are deliberate trade-offs, and knowing the
rationale makes it easier to tell a real gap from an intended behavior.

When you find a genuine correctness problem, the fix should land with a test that would have caught
it. That's what keeps it fixed.

---

## Deployment and trust model

survey-agent runs in one of two ways, both operated by a trusted party:

1. **Locally**, on the operator's own machine.
2. **Self-hosted**, on infrastructure the operator controls.

It is not designed as a public, multi-tenant service where untrusted people upload surveys or
configure integrations. The operator who writes the survey specs is the same party who deploys it.
That shapes the security boundary:

- **Survey specs and integration config are trusted operator input.** Enrichment `field`s, webhook
  URLs, SQL shapes and themes are configuration authored (and approved) by the operator.
- **The externally controlled inputs are URL query parameters and respondent answers.** URL
  parameters are HTML-escaped and can be HMAC-signed (`settings.url_signing`, `SURVEY_URL_SECRET`);
  answers are validated and HTML-escaped. That's where the hardening effort goes.
- **LLM-authored specs are safe to run, not automatically safe to field.** Logic is a closed AST with
  no `eval`, and the validate + simulate gates check a spec before it fields. Launching to real people
  still needs a human approval (see [the launch gate](#the-launch-gate)).

Defense-in-depth still applies to operator input where it's cheap. In the reference warehouse
adapter, SQL identifiers are validated and values are parameterized; regexes in validation and logic
are length/complexity-bounded at validate time and their input is length-capped.

## Who it's for: two roles in one workflow

The typical user is a small team, often one increasingly technical person, in two roles:

1. **Set up once (technical).** Stand up what fielding needs: a publicly reachable runtime for the
   `survey_url` (a cheap host, possibly scale-to-zero), a response store, and an erasure path.
2. **Operate continuously (research).** Author, test, launch and read results by talking to an agent.
   Authoring and testing are fully local and offline; only fielding uses the hosted runtime.

The agent-builder primitive (`begin_survey` / `answer_survey`, see [PRIMITIVE.md](../PRIMITIVE.md))
is a separate, secondary entry point.

**Laptop tunneling is not a hosting mode.** Fielding belongs on a hosted runtime (see
[HOSTING.md](../HOSTING.md)), not on a tunnel to a laptop that sleeps.

## Operator responsibilities are acknowledged, not implied

Storage, erasure, consent, URL integrity and transport security are the deployer's responsibility.
Rather than claim guarantees the engine can't make, `launch` prints an operator checklist, and in a
token-gated deployment the operator must acknowledge it (`--ack-responsibilities` /
`SURVEY_OPERATOR_ACK=1`) before a survey can go live. See
[OPERATOR_RESPONSIBILITIES.md](../OPERATOR_RESPONSIBILITIES.md).

## Consent is a disclosure notice

`consent_required` shows a disclosure notice; it doesn't hard-block the survey. In the intended
deployments (an operator fielding to people they invited) that's the appropriate behavior. Any
LLM processing of answers (`source: llm` analysis, conversation mode) is opt-in and disclosed.

## Reference integrations ship in the repo as examples

The Firebolt enrichment adapter, the Mailchimp integration and the Mongo store are reference
implementations of the `SURVEY_ENRICH`, `SURVEY_INTEGRATION` and `SURVEY_STORE` seams. They show
how to plug in a backend; a deployer swaps in their own via an import path (`your.pkg:Thing`).
Behavioral enrichment is database-agnostic: the docs describe it in terms of "your behavioral
database or warehouse", with Firebolt as one shipped adapter.

## Nothing brand- or vendor-specific in the core

A deployer's brand theme, assets, templates and surveys live in an **overlay** outside the package:
themes in `$SURVEY_THEMES_DIR` or `./overlay/themes`, templates in `$SURVEY_TEMPLATES_DIR`,
`./overlay/templates` or `./templates`. External themes are searched before the built-ins (so a
deployer can override one by name) and `base.css` always comes from the package. The core keeps the
mechanisms and brand-neutral reference content. See [TEMPLATES.md](TEMPLATES.md).

## The API gateway is the control plane

The console, the MCP connector and the CLI can reach the data backend through a FastAPI gateway
(`survey-agent api`, the `[api]` extra) instead of connecting to the database directly. The gateway
owns the database and exposes the definition and read-side store seams over HTTP. Clients use the
`http` backends (`SURVEY_DEFINITION_SOURCE=http`, `SURVEY_STORE=http`), which are pure standard
library, so clients gain no dependencies. The `http` store is read-only, and the gateway refuses to
back onto `http` itself (no self-loop).

The respondent runtime keeps a direct store on the hot path; the gateway is not in the respondent
request path.

**The gateway runs open unless you configure auth**: a bearer `SURVEY_API_TOKEN`, Google OAuth (the
`[auth]` extra), or `sa_` API keys. Configure one before exposing it.

## `live` is the serving gate

The runtime enforces the lifecycle. `serve --public` serves respondents only for surveys whose state
is in `SURVEY_SERVE_STATES` (default `live`). `paused` and `closed` show their own pages; earlier
states (`draft`, `validated`, `tested`, `approved`) show a "not open yet" page. This makes `live`
mean what operators expect: deployed and collecting.

- **Operator preview is token-gated and collects nothing.** A request carrying
  `preview=<SURVEY_PREVIEW_TOKEN>` bypasses the gate; those sessions record no response and fire no
  passback. With no token configured the gate can't be bypassed.
- **Local sandboxes serve every state.** `survey-agent demo` (and local preview mode) use
  `SURVEY_SERVE_STATES=*`, which the CLI refuses to combine with public hosting.
- **Marking live pins the version.** With the `mongo` definition source, respondents get the
  `live_version` until a re-publish; edits create a new draft version that doesn't reach the field
  until then. The `file` backend is one version per file, so it serves the current file.
- **Unmanaged surveys are served.** A survey with no registry state (the single-file `serve` path) is
  served as before; the gate applies once a lifecycle state exists. `SURVEY_SERVE_STATES` is also the
  lever for widening the gate temporarily during a migration.

The gate lives in `server.py`, keyed off the definition-store registry; the engine stays pure.

## The launch gate

Marking a survey live means real people can take it, so it needs a human:

- When `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface (CLI `launch` / `status --set live` /
  `publish`, the console, MCP) can mark a survey live without the token, plus the operator checklist
  acknowledgement.
- When it is unset, a human at the local CLI or console can mark a survey live. The MCP connector
  can't: it always requires the token.

The token comparison is constant-time on every surface.

## `launch` marks live; publishing is separate

`survey-agent launch` emits the drop-in URL and records the `live` state. It does not deploy. Surveys
are files under version control, and for many teams a reviewed merge *is* the approval step;
auto-deploying on `launch` would route around it. So:

- Plain `launch` warns that it did not publish and prints the publish command it detects.
- `launch --deploy` runs an operator-configured hook (`deploy.command` in `survey-agent.config.yaml`,
  or `$SURVEY_DEPLOY_COMMAND`).
- With a runtime definition store (`mongo` / `http`), `publish` writes the definition to the store, so
  no redeploy is needed.

Deploy makes a definition *reachable*; `live` makes it *open*. Publish logic lives only in the
CLI/operator layer; the engine assumes no particular git workflow or host.

**Surveys stay coupled to deploys.** Pointing `SURVEY_DIR` at a mutable disk or bucket so new surveys
appear without a deploy would bypass the reviewed-deploy model. Teams that need zero-downtime
launches use the Mongo-backed blueprint instead.

## Two Render blueprints; files stay the default

Render is one hosting option among several (see [DEPLOY.md](../DEPLOY.md)). Two blueprints ship:

- `render.yaml`: flat files on a persistent disk. Zero extra infrastructure, but not zero-downtime: a
  Render disk binds to one instance, so each deploy stops and starts it.
- `render.mongo.yaml`: diskless, with a Mongo store, giving rolling zero-downtime deploys for
  continuous fielding.

Both use a paid instance type (`plan: starter`; a persistent disk needs a paid instance). Files stay
the default to keep the "everything is files" on-ramp. `pymongo` is in its own `[mongo]` extra.

## Retakes are per invite link

`settings.retake` defaults to `"once"`: a finished `uid` + `rid` link is locked. `"multiple"` lets the
same link start a fresh attempt. Deduplication is by invite link, not by person. Each retake gets a
distinct response id (`session_id#N`), so its stored response and passback idempotency key don't
collide with the earlier attempt.

## Validation is the first gate; simulation is the behavioral gate

`validate` catches structural problems before a survey can field: duplicate question ids, choice
questions with no options, grids with no rows, incomplete `set_variable` actions, unknown references
in logic, `hidden[].calc` expressions and merge tokens (in titles, bodies, option labels, page titles,
webhook URLs and payloads, redirect URLs and screen-out messages), and display logic that depends on a
question on the same or a later page (it could never show). `simulate` then drives synthetic
respondents through every path; a survey that gets stuck is reported as a coverage problem rather than
crashing the simulator.

## Logic semantics

- **`set_variable value_from` copies the stored coded answer** (lists preserved), not a display label,
  so a derived variable stays comparable in later `equals` / `is_one_of` logic.
- **`equals` is set-based** (right for checkboxes). **`equals_ordered`** is order- and
  duplicate-sensitive, for rankings.
- **Piped-option answers export their labels.** A question whose options are piped from an earlier
  question inherits that source's value→label map, so export and `{{q:…}}` tokens show labels.

## Webhook actions are intents, delivered once

A mid-survey `webhook` action fires at most once per session. The engine stays network-free: it
returns a resolved intent with a stable `idempotency_key` (session + action index), and the caller
delivers it. The optional durable outbox (`SURVEY_OUTBOX=1`, `survey-agent outbox`) covers these
webhook actions; terminal passbacks go through the configured integration.

## Question and page repeating

- **A repeated answer is an ordinary answer under an instance id, `qid[item]`** (`q_rate[netflix]`).
  Logic, merge tokens, quotas, `set_variable value_from` and export read it with no special case.
  Authored ids can't contain brackets (schema pattern), so an instance id never collides with a real
  question, and a source's option values must be token-safe (letters, digits, `_`, `-`). There is no
  parallel "loop answers" structure.
- **The source is a choice question on an earlier page**, or a number with `by: value`. Exclusive
  options ("none of the above") are never repeated over. `max` defaults to 50 and is capped at 150.
  A question-level repeat can't sit inside a repeated page (no nesting).
- **Mid-loop terminals:** disqualify / overquota / complete end the survey immediately; `skip` waits
  until the loop ends, so a skip can't strand a half-finished loop. A repeated page's rendered id
  carries its item (`p_rate[netflix]`), so a stale Back-button submit re-renders the current pass
  instead of overwriting an earlier one.
- **The runtime renders a page's `title`** (piped), which on a repeated page is where the item's name
  usually goes.

## LLM calls are bounded and fail open

LLM calls pass a timeout and retry count (`SURVEY_LLM_TIMEOUT`, `SURVEY_LLM_RETRIES`), and authoring
retries once on an unparseable or truncated reply. A model failure never blocks a respondent.

## MCP sessions are process-local and bounded

`begin_survey` / `answer_survey` sessions live in the connector process's memory in an LRU-bounded
store (`SURVEY_MCP_MAX_SESSIONS`, default 256), so abandoned sessions don't accumulate. A
multi-worker deployment must route a session to one worker (see [PRIMITIVE.md](../PRIMITIVE.md)).

The `[mcp]` extra is pinned to `mcp>=1.2,<2`, because mcp 2.x renamed the server class the connector
is built on.

## Multi-clone editable installs are detected

A virtualenv allows one editable install per package, so a second clone silently runs the first
clone's code. `survey-agent doctor` and a startup warning catch this (`SURVEY_AGENT_NO_DOCTOR=1`
silences the warning).

## The operator console is a local tool

The console (`survey-agent console`) binds `127.0.0.1` and has no authentication of its own, by
design. If you expose it to a team, put your own authentication in front of it. See
[CONSOLE.md](../CONSOLE.md).
