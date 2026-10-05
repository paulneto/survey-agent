# Operator responsibilities (before you field to real people)

survey-agent is **run and hosted by you**, locally or as part of your own stack. It is not a managed
service, so several guarantees are *yours* to make, not the tool's. This is a short read; it's the
same checklist the `launch` command and the `launch_survey` MCP tool print. In a token-gated
deployment (`SURVEY_LAUNCH_APPROVAL_TOKEN` set) the operator must acknowledge it
(`--ack-responsibilities`, or `SURVEY_OPERATOR_ACK=1` for standing sign-off) before a survey can be
marked live. Marking live is lifecycle bookkeeping; respondents can reach the survey only once your
runtime serves it — see [DEPLOY.md](DEPLOY.md).

> Who this is for: the person who stands the instance up (see *Who it's for* in the
> [README](../README.md#who-its-for)). The person who authors surveys day to day doesn't need to
> re-read it each time; it's a one-time setup, then standing sign-off.

## The checklist

- **☐ Response store.** Choose a durable store and back it up. With `SURVEY_STORE` unset, responses
  go to the `file` store under `./survey_data` (override with `SURVEY_DATA_DIR`) — fine on a
  *persistent* disk, but on a scale-to-zero or ephemeral-filesystem host those files are lost on
  restart. There, use a database (the reference `mongo` store, or your own store plugin). `sqlite`
  suits several workers on one box. The `memory` store is for tests only. `survey-agent backup` /
  `restore` give you a portable copy of stored responses. See [HOSTING.md](HOSTING.md).
- **☐ Erasure / DSAR.** You must be able to find and delete a subject's responses.
  `survey-agent dsar <uid>` lists them and `survey-agent dsar <uid> --delete` erases them from the
  configured store (every built-in store implements `delete_responses`; a custom store must too).
  Copies outside the store — exports, backups, webhook/CRM/panel receivers, your warehouse — are yours
  to clear. Decide your retention policy before you collect anything.
- **☐ Consent & disclosure.** The survey's consent/notice text must be accurate for your
  jurisdiction and audience. If you enable **LLM analysis** (`source: llm`, `SURVEY_LLM_ENRICH`) or
  **conversation mode**, respondent data is sent to the model provider you configured — disclose it.
  Default notice text ships (`settings.ai.notice` / `convo_notice` override it); `requires_consent`
  can gate an LLM analysis on a consent question. A notice alone does not block anyone.
- **☐ URL integrity.** If any URL parameter affects **eligibility, quota, routing, or payment**,
  sign it: set `settings.url_signing.required` and mint per-respondent links with
  `survey-agent sign-url <survey> uid=… rid=…` (HMAC with the secret in `SURVEY_URL_SECRET`).
  Unsigned parameters can be edited in the address bar.
- **☐ Transport & access.** Serve the runtime over **HTTPS** (your host or proxy terminates TLS).
  The operator console (`survey-agent console`, or `/manage` with `SURVEY_CONSOLE=1`) is
  unauthenticated, and the API gateway (`survey-agent api`) runs **open unless you configure auth**
  (`SURVEY_API_TOKEN`, OAuth, or `sa_` API keys). Keep them off the public internet or behind your
  own auth.
- **☐ Passback reliability.** Terminal passbacks (`redirect`, `webhook`, `panel` integrations) are
  best-effort and carry an idempotency key. The optional durable outbox (`SURVEY_OUTBOX=1`) covers
  mid-survey webhook **actions** only. If a lost completion means an unpaid respondent, reconcile
  against the store (`/api/responses`, `export`) on your side.

## What the engine *does* give you

The engine evaluates the **logic** (screen-out / quota / branch / validation) as data, not a model's
judgment; keeps definitions **immutable and versioned** (in-flight respondents stay on their
version); signs outbound webhooks and panel passbacks and verifies signed URLs; records opt-in LLM
analysis in a per-response `llm_audit`; erases a subject's responses on request; and emits a
**stable export shape** your downstream tooling can rely on. Everything about *where the data lives,
who can reach it, how it's encrypted, and when it's deleted* is the deployer's call.

Full detail: [DATA_SECURITY.md](DATA_SECURITY.md). Hosting shapes: [HOSTING.md](HOSTING.md).
