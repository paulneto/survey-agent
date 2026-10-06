# Security Policy

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public issue, pull request, or discussion
for a suspected vulnerability.

- Use GitHub's private vulnerability reporting: go to
  [github.com/paulneto/survey-agent/security/advisories/new](https://github.com/paulneto/survey-agent/security/advisories/new)
  (or the repository's **Security** tab → **Report a vulnerability**). This opens a private security
  advisory visible only to you and the maintainers.

Please include a description of the issue, the affected version or commit, steps to reproduce, and
the impact you believe it has. This is a small project maintained on a best-effort basis: expect an
acknowledgement within a few days. The maintainers will work with you on a fix and a coordinated
disclosure date, and will credit you in the advisory unless you prefer otherwise.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (first public release) | Yes |
| anything older / unreleased snapshots | No |

survey-agent is early (0.x). Fixes land on `main` and in the next 0.1.x release.

## Security model: what the code does, and what it doesn't

**The engine does not execute survey content as code.** Survey logic is data: conditions are a
closed AST evaluated by `engine/ast_eval.py`, with no `eval`/`exec` and a fixed operator set.
Regular expressions in logic and validation are rejected at validate time if longer than 200
characters, built with nested unbounded quantifiers (`(x+)+`), or repeating an alternation
(`(a|aa)+`). At run time they match at most 2,000 characters of input on the `regex` engine with a
50 ms timeout per match. A match that times out is treated as no match in logic, and the check is skipped (answer accepted) in question validation, so a respondent is never blocked. Definitions must pass a JSON Schema
(`additionalProperties: false`) and a semantic lint before they run. This guarantee is about the
**engine**; plugins you install (stores, integrations, enrichment providers) are ordinary Python code
and run with your process's privileges.

**Trust model.** Survey specs are trusted operator input: an operator approves what gets fielded. The
validate + simulate gates are what make an LLM-authored spec safe to *run* (closed logic language, no
eval, every declared path coverage-checked). Fielding to real people still needs a human: when
`SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface (CLI, console, MCP, API gateway) can mark a survey
live without the token. When it is unset, the local CLI/console can mark a survey live; the MCP
connector and the API gateway cannot mark anything live at all. On the gateway, any registry write
whose result is live, and that newly goes live or changes `status`, `live` or `live_version`, needs
the token (`X-Survey-Launch-Approval` header or an `approve` field), and only the fields `status`,
`note`, `project`, `live`, `live_version`, `validated` and `tested` are writable.

**Respondent sessions.** Each respondent session gets an opaque random token, rendered into the
survey form. A submit must carry the session id *and* that token, and the session must belong to the
survey in the URL; anything else is refused. The invitation link (signed, if the survey requires
signed links) is how a respondent resumes. In-progress respondents stay on the definition version they
started on.

**Where respondent data can reach an LLM.** Authoring, validation, simulation and critique work on
survey *definitions*. Respondent data is sent to a model only in: conversation mode (the model reads
each reply), opt-in `source: llm` analysis at completion (`SURVEY_LLM_ENRICH`, recorded in the
response's `llm_audit`), and the operator-run `insights` read-out. Reporting/export tools (CLI or MCP)
return stored responses to whoever calls them, including an agent. You choose the model and key;
with no key configured, an offline stub runs and nothing leaves the machine. See
[docs/DATA_SECURITY.md](docs/DATA_SECURITY.md).

**Transport integrity.** Webhook posts are HMAC-SHA256 signed (`SURVEY_WEBHOOK_SECRET`,
`X-Survey-Signature: t=…,v1=…`, timestamp-checked on verify); panel passbacks are signed with
`SURVEY_PANEL_SHARED_SECRET`. Inbound URL parameters can be signed with `survey-agent sign-url` and
verified by the runtime (`SURVEY_URL_SECRET` + `settings.url_signing.required`). Terminal events
carry an idempotency key so receivers can dedupe. Passback events forward only the survey's declared
`settings.url_variables` plus the identity variables; every captured parameter is still stored on the
response.

**Request handling and logs.** The runtime caps request bodies (`SURVEY_MAX_BODY_BYTES`, default
10 MB; 64 KB for the chat JSON endpoint) and sets a per-request socket timeout
(`SURVEY_REQUEST_TIMEOUT`, default 30 s). Uploads are checked against the question's count, extension
and size rules, and must belong to a question on the respondent's current page, before any bytes are
stored; rejected uploads are deleted. Survey pages and data responses carry
`Cache-Control: no-store`, and every runtime response carries `Referrer-Policy: no-referrer` and
`X-Content-Type-Options: nosniff`. Logs carry hashed session and
response ids, and URLs reduced to scheme, host and path.

**Access control is mostly yours.**
- The **API gateway** (`survey-agent api`) runs **open, with no authentication, unless you configure
  it** (`SURVEY_API_TOKEN`, Google OAuth, or `sa_` API keys). Session cookies are honoured only when
  Google sign-in is configured with a strong `SURVEY_SESSION_JWT_KEY` (not the dev default, at least 32
  characters); with OAuth configured and a weak key, the gateway refuses to start.
- The **operator console** binds `127.0.0.1` and is unauthenticated by design.
- The runtime's `/api/responses` endpoint is disabled until `SURVEY_API_TOKEN` is set, then requires it
  in the `Authorization: Bearer` header (a `?token=` query parameter is not accepted).
- The browser **chat** at `/chat` is a preview: it is served only in demo/preview mode (or with
  `SURVEY_CHAT_DEMO=1`) and records nothing.

**Not provided by the code** (deployer responsibilities): encryption at rest, PII redaction (exports
are not redacted), audit logging beyond `llm_audit`, organization/tenant scoping, OAuth for a remote
MCP connector, and regulatory compliance (GDPR/CCPA etc.). `survey-agent dsar <id> --delete`
erases a subject's responses, sessions, uploaded files and queued webhook deliveries; copies already
delivered outside the app (passbacks, backups, exports, logs, warehouses) are yours to clear.

**Rendering.** The bundled renderer HTML-escapes piped answers. A custom frontend must escape piped
free text itself.

## Out of scope

- Your deployment's infrastructure: TLS, secrets management, network policy, the store you choose,
  and any auth proxy in front of the console, gateway, or MCP connector.
- Third-party services you integrate (email/CRM, your warehouse, your panel) and their data handling.
- Vulnerabilities in third-party plugins not shipped in this repository.
