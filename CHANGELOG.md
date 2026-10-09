# Changelog

All notable changes to **survey-agent** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may include breaking changes).

## [0.1.0] — 2026-10-09

First public release.

### Added

**Surveys as data**
- A survey is one JSON document (`.survey.json`), validated against a published JSON Schema
  (`survey-agent schema`). A friendlier Markdown form compiles to it 1:1 (`compile`), and `render`
  turns JSON back into readable, logic-annotated Markdown.
- Definitions are immutable and versioned; an edit creates a new version and in-flight sessions stay
  pinned to the version they started on. The `file` source keeps each version as a snapshot in
  `surveys/.versions/<id>/v<N>.survey.json`. Identical content keeps its version; changed content
  never reuses a number. If a session's version is no longer available, the respondent is asked to
  reopen their link, which restarts them on the live version.
- Saving a definition never changes what respondents are served. The registry reports the approved
  `live_version` and the newest saved `latest_version` separately, and only an approved mark-live
  (`launch`, `publish`, `status --set live`, the console's Mark live, a gated gateway registry write)
  moves `live_version`; one that names no version pins the newest saved one. With `file` definitions,
  an edited file keeps serving the approved snapshot until it is launched or published, so
  `surveys/.registry.json` and `surveys/.versions/` deploy together.
- Survey ids must match `^[A-Za-z0-9_-]+$`, and the `file` definition backend refuses paths outside
  its surveys directory.
- Question types: radio, checkbox (with an exclusive "none of the above"), dropdown, multi-select
  dropdown, short text (regex / email / phone / length / `min_words`), long text, number
  (min/max/percent), rating / Likert, NPS, ranking, grid / matrix (radio or checkbox cells), date, and
  file upload (extension / size / count validated before anything is stored, each upload under a
  unique key in a pluggable local or S3 blob store; rejected uploads are deleted).
  Display-only image, video and instructions blocks render formatted Markdown.
- Logic as a closed JSON AST, evaluated without `eval`: show/hide, page display, skip / branch,
  disqualify / screen-out, per-option and grid-row logic, nested AND/OR, and a fixed operator set.
  Regular expressions are length- and complexity-bounded at validate time (no nested unbounded
  quantifiers or repeated alternations), run on the `regex` engine (a core dependency) with a 50 ms
  timeout per match, and match at most 2,000 characters of input. If `regex` is missing, survey
  patterns are refused rather than run unbounded.
- Piping and merge tokens for answers, URL variables, variables and context facts (`{{q:…}}`,
  `{{url:…}}`, `{{var:…}}`, `{{ctx:…}}`), with fallbacks (`{{q:q_fav|your favourite}}`) and option
  piping that carries selected options forward. Piped values are shown as plain text.
- Question and page repeating: ask a question or a whole page once per item from an earlier answer
  (selected or not-selected options) or N times from a number. Each item's answer is an ordinary key
  (`q_rate[netflix]`), so logic, piping, quotas and export need no special case.
- Seeded, per-session randomization with locked positions; overall and per-option quotas, where a
  completion atomically reserves every bucket it falls into and becomes the over-quota ending if any
  is full; hidden values and calculations (score / sum / bucket / count / arithmetic) usable in
  logic, piping, quotas and routing; mid-survey webhook actions (decided once per session, idempotency-keyed, HMAC-signed).
- `settings.identity` names the URL variables that carry respondent and response identity (default
  `uid` and `rid`); `settings.retake` (`once` or `multiple`) controls whether an invite link can be
  taken again.

**Gates before fielding**
- `validate`: schema plus semantic lint (references, page ordering, regex bounds, and more).
- `simulate`: a coverage gate that drives synthetic respondents and requires that a qualified
  respondent reaches complete and that every declared screen-out is reachable.
- `pretest`: role-played persona respondents for an incidence and length preview, against a synthetic
  or declared audience, or your own persona source.
- `critique`: an LLM methodology review (leading, double-barreled, missing opt-outs, ranges).
- `author`: draft a survey from a plain-language brief, then repair it until it passes validation,
  the coverage gate and, when the brief states them, incidence and length targets.

**Running surveys**
- A standard-library HTTP runtime (`serve`, `serve --multi`) that emits a drop-in `survey_url` and
  captures every query parameter. `serve --public` serves `live` surveys only; `serve --preview`
  serves drafts locally; `demo` runs the runtime with a mock receiver.
- Respondent sessions carry an opaque per-session token; a submit must match it and the survey in the
  URL, and the invite link is how a respondent resumes. A session's ending is processed once.
- Retry-safe completions: quota reservation is keyed by the response id (a repeat returns the
  original outcome), responses are upserted by id on every built-in store, the session is marked
  recorded only after the write succeeds, and a completion interrupted part-way is finished on the
  respondent's next request. Passbacks and webhooks run after the response is recorded and are
  best-effort: a failure is logged, not shown to the respondent, and a repeat carries the same
  idempotency key.
- Request limits (`SURVEY_MAX_BODY_BYTES`, default 10 MB; `SURVEY_REQUEST_TIMEOUT`, default 30 s),
  `Cache-Control: no-store` on survey pages and data, `Referrer-Policy: no-referrer` and
  `X-Content-Type-Options: nosniff`, and logs that hash session/response ids and reduce URLs (`url`
  and any `*_url` field) to `scheme://host[:port]#<fingerprint>`, never the path, query or userinfo.
- Lifecycle states (draft → validated → tested → approved → live → paused / closed), tracked in a
  registry sidecar; only serving states reach respondents, and `paused` / `closed` stop fielding.
- Conversational delivery: the same survey administered as a chat, with the engine deciding flow,
  screen-outs and quotas, and the LLM only phrasing questions and mapping replies. `convo` and the
  browser `/chat` (served only in demo/preview mode or with `SURVEY_CHAT_DEMO=1`) are previews that
  record nothing; agents collect answers with `begin_survey` / `answer_survey`.
- Signed URL parameters (`sign-url`, `settings.url_signing.required`) so eligibility values can't be
  edited in the address bar. The signature is an HMAC over a domain-separated canonical JSON of the
  sorted `[name, value]` pairs (a missing field differs from an empty one; `sign-url` refuses blank
  values). Signing fails closed: while `SURVEY_URL_SECRET` is unset or blank, a survey that requires
  it refuses every invitation (403), and the startup preflight names the affected surveys.
- A human launch gate: when `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface can mark a survey live
  without it; the MCP connector and the API gateway always require it.

**Storage and integrations**
- Response stores: `file` (default), `sqlite`, `memory`, a reference `mongo` backend, `http` (via the
  API gateway), or your own class by import path.
- Definition sources: `file` (default), reference `mongo`, `http`, or your own, so a survey can be
  published without a redeploy.
- Completion integrations that deliver neutral lifecycle events with an idempotency key: `none`
  (default), `redirect`, `webhook`, a generic `panel` passback, a reference `mailchimp` adapter, or
  your own plugin. Events forward only the survey's declared `settings.url_variables` plus the
  identity variables; every captured parameter is still stored on the response.
- An optional durable outbox for webhook actions (`SURVEY_OUTBOX=1`, `outbox`).
- Context enrichment: a `static` fixture provider, a reference warehouse adapter, or your own
  provider by import path. Optional `source: llm` analysis of open text runs at completion and is
  stored on the response.
- A behavioral-trigger sidecar that posts a generic launch request to your panel endpoint.

**Operating and reporting**
- `report` (self-contained HTML: funnel, incidence, length, per-question charts, NPS, segments, quota
  burndown), `insights` (an AI analyst read-out), `export` (CSV or Parquet), `dashboard` (an HTML
  index of every survey, with `--live` counts), `list`, `status` and projects for grouping surveys.
- `console`: a local operator console with a board of surveys, preview, launch checks, lifecycle
  controls and response download.
- Templates: `templates`, `new`, `copy` and `templatize`, with `${name}` parameters.
- Data subject requests: `dsar <id>` finds a respondent's responses and sessions, matched on every
  identity variable in use (`--id-field` adds more; `--json --full` gives the access copy), and
  `--delete` erases them along with their uploaded files and queued webhook deliveries. Copies
  delivered outside the app (passbacks, backups, exports, logs, warehouses) must be erased there.
  Erasure writes a durable job manifest first (the subject id and identity fields so it can be
  retried, never answers; mode 0600, deleted when the job completes). A job is complete only once
  the store confirms the records are erased and every file and queued delivery is gone; until then
  `dsar` exits non-zero and the next run (or `dsar --resume`) finishes it, re-running the record
  erasure if it was interrupted.
  `backup` / `restore` for responses (restore never duplicates a response); `defs-export` / `defs-import` / `defs-diff` for definitions.
- `doctor` (resolved install, configuration and deployment checks), `help`, `docs`, `init`,
  `selftest`.

**Agent surfaces**
- An MCP connector (`survey-agent-mcp`) exposing the authoring, testing, launch and reporting loop as
  tools, plus the schema, catalog, themes and templates as resources.
- `begin_survey` / `answer_survey`: a structured-elicitation primitive that lets any agent collect
  logic-gated answers from a person mid-task.
- Claude Code skills: author-survey, edit-survey, add-logic, test-survey, launch-survey,
  survey-report and inline-survey.
- Bring your own model: Anthropic Claude or an OpenAI-compatible model, with an offline stub for
  tests and demos.

**API gateway**
- `survey-agent api` (the `[api]` extra): an HTTP control plane for definitions and responses. It
  runs without authentication unless you configure a bearer token, Google OAuth (the `[auth]` extra)
  or `sa_` API keys. `[api]` alone runs bearer-token mode; the `/keys` page needs Google sign-in.
  Session cookies are honoured only with Google sign-in and a strong `SURVEY_SESSION_JWT_KEY` (the
  gateway refuses to start with a weak one, or without `[auth]` installed). Registry writes accept an
  allowlist of fields, and any write that makes or changes what is live needs the launch approval.
  `PUT /v1/definitions` validates the spec (422 with the errors; the body is `{"survey": {...}}`) and
  never changes what is live.

**Hosting**
- A Dockerfile, a `Procfile`, Render blueprints, a Fly.io config and reference Terraform. The runtime
  derives its public URL from common hosts, and `doctor` reports whether it is reachable.
- An allowlist `.dockerignore` (only the package source, packaging files, survey definitions,
  registry, version snapshots and triggers reach the image), a `.gitignore` that keeps uploads,
  `.env.*` files, local databases, backups, reports and exports out of git, and a `deploy.sh` that
  stages only definition paths (`DEPLOY_PATHS` to override) and refuses to commit anything else.
