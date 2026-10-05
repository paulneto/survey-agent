# Data Security Guidance

How survey-agent handles data, where the risks are, and the controls a deployer must put in place.
survey-agent touches **respondent answers, pseudonymous identifiers, and (optionally) behavioral
data**, so treat security as part of the deployment, not an afterthought.

This page separates two things clearly: **what the code does today**, and **what you, the deployer,
are responsible for**. Where a control is listed as yours, survey-agent does not provide it.

## What data flows through it (classify it first)

| Data | Sensitivity | Where it lives |
|---|---|---|
| Respondent **answers** (incl. free text, file uploads) | **High** (may contain PII) | the response store (sessions + responses); upload bytes in the upload store |
| **URL variables** (`uid`, `rid`, `group`, utm…) | Medium (pseudonymous identifiers) | `url_variables` on every response |
| **Network metadata** (IP, user agent, referer, fingerprint) | Medium–high | on the response under `client`, **only** if you set `SURVEY_CAPTURE_CLIENT_META=1` |
| **Behavioral enrichment** (e.g. recent searches or app use from your warehouse) | **High** (can be health/financial/intimate) | resolved at session start into the session; `source: llm` analysis values are stored on the response under `derived` |
| Quota counters / aggregate stats | Low | the store's quota counters |
| Survey **definitions** | Low (no respondent data) | files in git and/or a definition store |

## Core principles

1. **Data minimization.** Capture only what the study needs; declare enrichments narrowly.
2. **Least privilege.** Read-only, scoped database/warehouse credentials for enrichment; give the
   public runtime only what it needs.
3. **Encrypt in transit and at rest.** Both are deployment concerns (see below).
4. **Know exactly where respondent data meets an LLM** (next section), and disclose it.
5. **Trust no inbound parameter that affects eligibility or payment** unless it's signed.
6. **Be able to find and erase a subject's data.**

## Trust model

- **Survey specs are trusted operator input.** An operator approves what gets fielded.
- **The gates make LLM-authored specs safe to run.** A spec must pass the JSON Schema and semantic
  lint (`validate`) and the coverage gate (`simulate`). Logic is a closed AST with no `eval`, so an
  LLM-written survey cannot make the **engine** run arbitrary code. (Plugins you install are ordinary
  Python and are trusted code — see *Supply chain*.)
- **Fielding to real people needs a human.** When `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface
  (CLI, console, MCP) can mark a survey live without the token. When it is unset, the local CLI and
  console can mark a survey live; the MCP connector always requires the token.

## Where respondent data can reach an LLM

| Surface | What the model sees | When |
|---|---|---|
| Authoring / `author`, `critique`, `validate`, `simulate` | survey **definitions** only | design time |
| `pretest` (persona pre-test) | the definition plus **synthetic** personas' answers — no real respondent data | design time |
| **Conversation mode** (`/chat`, `convo`, `begin_survey`/`answer_survey`) | each respondent **reply** (the model reads it to record the answer, and may ask one follow-up on a thin open answer) | while the respondent answers, whenever a model key is configured |
| **`source: llm` analysis** | the spec's declared `inputs` from that response | **at completion**, only if `SURVEY_LLM_ENRICH` is enabled and the spec opts in; stored on the response, recorded in `llm_audit` |
| **`insights`** / `report --insights` / `survey_insights` | aggregate stats plus a sample of open-text answers | when an operator (or agent) runs it |

Notes:

- With **no model key configured**, a deterministic offline stub runs in every case and nothing is
  sent anywhere.
- `source: llm` analysis does **not** drive live logic, piping or routing and is not shown on screen
  mid-survey. It fails open: an error or timeout yields an empty value and never blocks the respondent.
  Only the declared `inputs` are sent. `requires_consent` can make it conditional on a consent question.
- Conversation mode always shows an AI disclosure at the start (default text, override with
  `settings.ai.convo_notice`). It is not recorded in `llm_audit`; adaptive follow-ups are stored on
  the session under `probes`.
- **Reporting and export tools return stored responses to their caller.** If you connect an agent
  to the MCP server and it calls `survey_report`, `survey_insights` or `export_survey` against a real
  store, that agent (and its model provider) sees response data. Use `simulate=N` for synthetic data
  when real data isn't needed.
- The model provider's data terms apply to anything sent. You choose the provider and key.

## Controls

### Transport & at rest (deployer)
- **HTTPS only.** The runtime speaks plain HTTP; terminate TLS at your host, load balancer, or proxy.
- **Encryption at rest is not provided by survey-agent.** Use your platform's disk/database/object
  storage encryption for the response store, uploads, backups, and export files.
- **Secrets** (store URIs, `SURVEY_URL_SECRET`, `SURVEY_WEBHOOK_SECRET`, `SURVEY_PANEL_SHARED_SECRET`,
  `SURVEY_API_TOKEN`, the launch-approval token, warehouse credentials, model keys) belong in your
  host's secret manager — never in git, survey JSON, or logs. The runtime's startup preflight refuses
  to start a fielding integration that still uses the dev-placeholder signing secret.

### URL parameters (integrity)
- The runtime captures every query parameter into `url_variables` and HTML-escapes them on display.
  Captured is not verified: **the moment a parameter affects screen-out, quota, routing, or payment,
  sign it.** Mint a per-respondent link with `survey-agent sign-url <survey> uid=… rid=… [group=…]`
  (or `survey_agent.engine.signing` in code) and set `settings.url_signing.required` so the runtime
  checks the signature. The secret lives in `SURVEY_URL_SECRET`, never in the survey JSON.
- Respondents have **no login**; identity is the (ideally signed) `uid`/`rid`.

### Behavioral enrichment (high-sensitivity path)
- Built-in providers are `static` (fixtures for demos/tests) and `firebolt` (a reference warehouse
  adapter); anything else is an adapter you write (`SURVEY_ENRICH=your.pkg:Provider`).
- In the reference adapter, enrichment specs are data compiled to SQL: **SQL identifiers are
  validated and values are parameterized.** Identifiers must match `^[A-Za-z_][A-Za-z0-9_]*$` and be
  allow-listed against the event catalog's fields; every value (including the respondent id) is bound
  as a parameter, never interpolated. An adapter
  you write must do the same.
- Use **read-only, least-privilege** credentials scoped to the needed tables. The reference adapter
  uses a short timeout and fails open (a missing fact hides the gated question; it never errors the
  respondent).
- **Don't log enrichment values.** If you plug in a cache, give it a short TTL.

### Outbound webhooks (the `webhook` action)
A `webhook` action sends data **out** mid-survey. Treat its payload as a deliberate disclosure.
- **The engine never makes the call.** `submit()` returns a resolved intent (`{url, method,
  payload}`); the runtime delivers it via `integrations.webhook.deliver`. That gives you one place to
  enforce egress policy.
- **Allowlist destinations.** survey-agent does not restrict webhook URLs; an authored survey can name
  any URL. Review webhook targets before approving a survey, and consider a network egress allowlist.
- **Minimize the payload.** Merge tokens (`{{q:…}}`, `{{url:…}}`, `{{var:…}}`) are resolved into it;
  send only what the receiver needs.
- **Signed.** With `SURVEY_WEBHOOK_SECRET` set, outbound webhook POSTs carry
  `X-Survey-Signature: t=<ts>,v1=<HMAC-SHA256 of "{ts}.{body}">`. Receivers verify with
  `integrations.webhook.verify(secret, body, header)`, which also rejects stale timestamps (default 300s).
- **Fire-once, fail-open.** A webhook action fires at most once per session and never blocks the
  respondent. For retries, enable the durable outbox (`SURVEY_OUTBOX=1`, inspect with
  `survey-agent outbox`); it covers webhook **actions** only, not terminal passbacks.
- Terminal passbacks go out only through the integration you select; with `SURVEY_INTEGRATION`
  unset (`none`) the runtime makes no outbound passback calls.

### Video & external stimulus
The `video` type is display-only and stores no respondent data, but it embeds third-party content:
- YouTube URLs render via `youtube-nocookie.com`; other URLs render in a plain `<video>` tag.
- Scope your CSP `frame-src`/`media-src` to the hosts you use; prefer assets you control for
  sensitive studies.

### PII in responses (deployer)
- **Free text may contain PII.** survey-agent does **not** redact anything — not in the store, not in
  `export`, not over `/api/responses`. If you need redaction or classification, run it in your own
  pipeline (for example, a custom store or a post-export step).
- **Access to raw data:** `export`, `report`, `backup` and the MCP reporting tools read whatever store
  they're pointed at; `/api/responses` requires `SURVEY_API_TOKEN`. Role-based access and audit
  logging of exports are not provided — add them at your gateway/proxy if you need them.
- **Right to erasure / DSAR: built in for the store.** `survey-agent dsar <uid>` lists a person's
  responses in the stable export shape; `survey-agent dsar <uid> --delete` erases them (`--survey ID`
  to limit, `--json` for a scriptable record). Every built-in store implements
  `delete_responses(uid, survey_id)`; a custom store must too. **Still yours:** backups, exports,
  copies already delivered to webhooks/panels/CRMs, and downstream warehouses.
- **Retention:** survey-agent does not expire responses or sessions. Define a retention policy and
  enforce it (e.g. a scheduled `dsar --delete`, or store-level TTLs).

### Network & access control (deployer)
- **Respondent runtime** (`survey-agent serve`): the only piece that must be public. It doesn't need
  model keys unless you use conversation mode or `source: llm` analysis.
- **API gateway** (`survey-agent api`): **runs open — no authentication — unless you configure it**
  with `SURVEY_API_TOKEN` (bearer), Google OAuth (`[auth]` extra, `SURVEY_OAUTH_*`), or `sa_` API
  keys. Don't expose it without one of these.
- **Operator console** (`survey-agent console`, `/manage` with `SURVEY_CONSOLE=1`): binds `127.0.0.1`
  and is unauthenticated by design. Put your own auth (proxy, VPN) in front if you expose it.
- **Organization/tenant scoping is not provided.** Anyone with access to a store or gateway sees all
  surveys and responses in it. Separate deployments or stores if you need isolation.
- Optional per-IP rate limiting on survey routes: `SURVEY_RATE_LIMIT` (e.g. `60/min`).

### Integrity & delivery
- Terminal passbacks carry an **idempotency key** (the response id) so receiver retries are safe.
  They are best-effort; reconcile against the store if a lost passback matters (e.g. payment).
- **Anti-fraud settings** (`settings.anti_fraud`: attention checks, duplicate-uid blocking, …) are
  declared in the schema and checked for references, but are **not enforced by the engine** yet.
  Today, implement an attention check as an ordinary disqualify rule. `settings.retake` (`once` by
  default) **is** enforced: a finished invite link can't be retaken.

### Observability (without leaking)
- The runtime logs via the `survey_agent` logger (`SURVEY_LOG_LEVEL`, `SURVEY_LOG_FORMAT=json`) and
  logs identifiers and statuses, not answers. Keep any custom code to the same rule: **never log
  answers, free text, enrichment values, or full `url_variables`.**
- **Audit logging** of authoring actions, exports and erasures is **not provided** beyond the
  per-response `llm_audit`. If you need it, record it at your gateway, proxy, or CI.

### Supply chain
- The engine is small (about 2,600 lines) with minimal dependencies (standard library plus
  `jsonschema` and `PyYAML`).
- **Plugins are code** (stores, definition sources, integrations, enrichment providers loaded by
  import path). They run in your process and can read responses. Vet, pin, and review them.

### Connectors (the agentic surface)
- The MCP server runs locally over stdio by default. For a remote connector
  (`--transport streamable-http`), **you** must put OAuth in front; survey-agent does not ship MCP OAuth.
- The launch gate applies: the MCP connector cannot mark a survey live without the approval token.

## Compliance posture
- survey-agent provides building blocks: consent/notice text, signed URLs and webhooks, a stable
  uid-indexed export, and store-level erasure.
- **It does not make a deployment GDPR/CCPA (or any other regime) compliant.** Lawful basis,
  consent wording, storage location, encryption, retention, processor agreements with your model
  provider, and regulatory classification are the deploying organization's responsibility.

## Pre-launch security checklist
- [ ] HTTPS enforced; at-rest encryption on the store, uploads, backups and exports; secrets in a
      manager (none in git/logs).
- [ ] URL params that affect eligibility/quota/pay are **signed** (`sign-url`) and verification is
      **required** (`settings.url_signing.required`).
- [ ] Enrichment uses **read-only, scoped** credentials; values **not logged**.
- [ ] Launch approval token set (`SURVEY_LAUNCH_APPROVAL_TOKEN`) if anyone other than a human
      operator can reach the CLI/console.
- [ ] LLM use reviewed: conversation mode, `source: llm` analysis and `insights` are disclosed in the
      consent/notice text; you're comfortable with the provider's data terms.
- [ ] Redaction (if needed) handled in your own pipeline; `dsar <uid> --delete` tested against your
      store; plan for backups and downstream copies.
- [ ] API gateway has auth configured; console not publicly reachable.
- [ ] Webhook targets reviewed; outbox enabled if webhook actions must not be lost.
- [ ] Retention policy defined and enforced.
- [ ] Any installed **plugins reviewed and pinned**.
