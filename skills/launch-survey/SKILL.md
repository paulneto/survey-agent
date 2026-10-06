---
name: launch-survey
description: Emit a survey-agent survey's drop-in survey_url for the operator to put in their own system (panel/CRM/email), and mark it live. Triggers on "launch the survey", "publish the screener", "go live", "field this survey", "give me the survey url".
---

# launch-survey

Produce the **drop-in `survey_url`** an operator pastes into their own system (panel, CRM or email
link) and mark the survey **live**. survey-agent never contacts anyone: launching emits the URL and
records state. Finding respondents, inviting them and paying any incentive is your own system's job.
Do the human review first.

## Preconditions (refuse if not met)

- `survey-agent validate surveys/<slug>.survey.json` is clean.
- `test-survey` passed (the coverage gate is green).
- A human has reviewed and approved the survey.

## The launch gate

Marking a survey live means real people can take it, so it is gated:

- **When `SURVEY_LAUNCH_APPROVAL_TOKEN` is set**, no surface can mark a survey live without the token:
  CLI `launch` / `status --set live` / `publish`, the console, and the MCP connector all refuse. On the
  CLI a human passes `--approve <token>` **and** acknowledges the operator checklist with
  `--ack-responsibilities` (or `SURVEY_OPERATOR_ACK=1` for a deployment with standing sign-off). Over
  MCP, `launch_survey` takes `approval_token` and `acknowledge_responsibilities=true`.
- **When it is unset**, a human at the local CLI or console can mark a survey live. The MCP connector
  and the API gateway still can't: they always require the token. (A CLI pointed at a gateway with
  `SURVEY_DEFINITION_SOURCE=http` forwards the `--approve` token it verified.)

You always get the `survey_url`. Only flipping the survey to `live` is gated. Never pass the token on
the human's behalf; show them the checklist and let them supply it.

## Steps

1. **Emit the URL (and mark live if approved):**
   `survey-agent launch surveys/<slug>.survey.json [--approve TOKEN --ack-responsibilities]` (or the
   `launch_survey` MCP tool). It validates, builds
   `<base>/s/<id>?uid={uid}&rid={rid}&group={group}` from the survey's declared `url_variables`, prints
   it with the operator checklist, and marks the survey **live** in the registry if allowed.
   `--no-live` just prints the URL.
   - **The base URL** comes from `--base`, else `SURVEY_PUBLIC_BASE_URL`, else it's derived from the
     host the runtime is on (e.g. `RENDER_EXTERNAL_URL`, `FLY_APP_NAME`, `RAILWAY_PUBLIC_DOMAIN`), else
     `http://localhost:8000`. A localhost base reaches nobody, and `launch` warns about it.
     `survey-agent doctor` shows the resolved URL and where it came from.
2. **Make sure the hosted runtime has the survey: deploy (reachable) vs. live (open).** These are
   separate. `live` is the runtime's serving gate: `serve --public` serves respondents **only** surveys
   in a state listed in `SURVEY_SERVE_STATES` (default `live`), and anything earlier shows a "not open
   yet" page. Marking live also pins the launched version.
   - With a runtime definition store (`SURVEY_DEFINITION_SOURCE=mongo` or `http`),
     `survey-agent publish` writes the definition to the store, so there's nothing to redeploy.
   - With the default `file` source, the file must also reach the hosted runtime. Run your deploy step
     (e.g. `git push` to an auto-deploying host), or configure a one-shot hook (`deploy.command` in
     `survey-agent.config.yaml`, or `$SURVEY_DEPLOY_COMMAND`) and use `survey-agent launch --deploy`,
     which runs it after marking live. Plain `launch` warns that it did not publish and prints the
     publish command it detects.
3. **Hand it to the operator:** they put the URL into their panel, CRM or email tool and replace each
   `{placeholder}` with their per-respondent value (`{uid}` becomes that person's id). Rename the
   variables in `settings.url_variables` (and `settings.identity`) to match whatever names their
   system sends. If the parameters must not be forgeable, set `settings.url_signing.required` and mint
   per-respondent links with `survey-agent sign-url surveys/<slug>.survey.json uid=… rid=…`
   (signed with `SURVEY_URL_SECRET`).
4. **Freeze the version:** commit the `.survey.json` and `surveys/.versions/` (the immutable
   version snapshots; `survey-agent publish` writes them). Definitions are immutable and versioned;
   in-flight respondents stay pinned to the version they started, as long as the deployed runtime
   has that version's snapshot.
5. Hand off to `survey-report` for monitoring.

## Optional: telling your system what happened

The terminal of each response (started / completed / terminated, plus the reason code) can be passed
back through an integration (`SURVEY_INTEGRATION`): `redirect` sends the respondent's browser to your
complete / screen-out URL, `webhook` posts the event to an endpoint, `panel` is a generic signed
passback, or plug in your own. It is `none` when unset. See [docs/ENGINEERING.md](../../docs/ENGINEERING.md).

## Notes

- Never auto-launch from a behavioral trigger without a pre-approved survey.
- Moving from another survey tool? Point your distribution at the new `survey_url` and compare
  complete / screen-out rates against the old copy before retiring it.
