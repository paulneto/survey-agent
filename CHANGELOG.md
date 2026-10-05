# Changelog

All notable changes to **survey-agent** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may include breaking changes).

## [0.1.0] — Unreleased

<!-- release date: 2026-10-XX -->

First public release.

### Added

**Surveys as data**
- A survey is one JSON document (`.survey.json`), validated against a published JSON Schema
  (`survey-agent schema`). A friendlier Markdown form compiles to it 1:1 (`compile`), and `render`
  turns JSON back into readable, logic-annotated Markdown.
- Definitions are immutable and versioned; an edit creates a new version and in-flight sessions stay
  pinned to the version they started on.
- Question types: radio, checkbox (with an exclusive "none of the above"), dropdown, multi-select
  dropdown, short text (regex / email / phone / length / `min_words`), long text, number
  (min/max/percent), rating / Likert, NPS, ranking, grid / matrix (radio or checkbox cells), date, and
  file upload (extension / size / count validated, stored in a pluggable local or S3 blob store).
  Display-only image, video and instructions blocks render formatted Markdown.
- Logic as a closed JSON AST, evaluated without `eval`: show/hide, page display, skip / branch,
  disqualify / screen-out, per-option and grid-row logic, nested AND/OR, and a fixed operator set.
  Regular expressions are length- and complexity-bounded at validate time, and the input they run on
  is capped.
- Piping and merge tokens for answers, URL variables, variables and context facts (`{{q:…}}`,
  `{{url:…}}`, `{{var:…}}`, `{{ctx:…}}`), with fallbacks (`{{q:q_fav|your favourite}}`) and option
  piping that carries selected options forward. Piped values are shown as plain text.
- Question and page repeating: ask a question or a whole page once per item from an earlier answer
  (selected or not-selected options) or N times from a number. Each item's answer is an ordinary key
  (`q_rate[netflix]`), so logic, piping, quotas and export need no special case.
- Seeded, per-session randomization with locked positions; overall and per-option quotas enforced
  atomically; hidden values and calculations (score / sum / bucket / count / arithmetic) usable in
  logic, piping, quotas and routing; mid-survey webhook actions (fire once per session, HMAC-signed).
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
- Lifecycle states (draft → validated → tested → approved → live → paused / closed), tracked in a
  registry sidecar; only serving states reach respondents, and `paused` / `closed` stop fielding.
- Conversational delivery: the same survey administered as a chat (`convo`, `/chat`), with the engine
  deciding flow, screen-outs and quotas, and the LLM only phrasing questions and mapping replies.
- Signed URL parameters (`sign-url`, `settings.url_signing.required`) so eligibility values can't be
  edited in the address bar.
- A human launch gate: when `SURVEY_LAUNCH_APPROVAL_TOKEN` is set, no surface can mark a survey live
  without it; the MCP connector always requires it.

**Storage and integrations**
- Response stores: `file` (default), `sqlite`, `memory`, a reference `mongo` backend, `http` (via the
  API gateway), or your own class by import path.
- Definition sources: `file` (default), reference `mongo`, `http`, or your own, so a survey can be
  published without a redeploy.
- Completion integrations that deliver neutral lifecycle events with an idempotency key: `none`
  (default), `redirect`, `webhook`, a generic `panel` passback, a reference `mailchimp` adapter, or
  your own plugin.
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
- Data subject requests: `dsar <uid>` finds a respondent's responses, and `--delete` erases them.
  `backup` / `restore` for responses; `defs-export` / `defs-import` / `defs-diff` for definitions.
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
  or `sa_` API keys.

**Hosting**
- A Dockerfile, a `Procfile`, Render blueprints, a Fly.io config and reference Terraform. The runtime
  derives its public URL from common hosts, and `doctor` reports whether it is reachable.
