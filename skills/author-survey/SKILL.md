---
name: author-survey
description: Turn a natural-language survey brief into a validated survey-agent Survey JSON spec. Triggers on "write a screener", "build a survey for…", "create a questionnaire", "draft a survey", or describing an audience to screen and route into a task.
---

# author-survey

Turn a plain-language brief into a **validated `surveys/<slug>.survey.json`** (plus the readable
`surveys/<slug>.md`) for the survey-agent engine. Do NOT launch anything — that's `launch-survey`.

## Steps

1. **Clarify (ask only what's missing):** target audience & screen-out criteria; quota cells +
   caps; estimated length (LOI) & incidence; what qualified respondents route into (a task URL?);
   any behavioral enrichment ("ask why they searched X"); theme (`paper` default, `dark`, `ocean`,
   `base`, or your own via `SURVEY_THEMES_DIR`).
   Starting from a common shape? `survey-agent templates` lists scaffolds and
   `survey-agent new <template> --slug <slug>` (MCP: `list_templates` / `new_survey`) writes a valid
   starting point to refine. `survey-agent author "<brief>"` drafts, validates and simulates in one
   closed loop (needs a model key).
2. **Draft the spec** against `engine/schema/survey.schema.json`:
   - Pages hold questions; a question that depends on a *prior* answer goes on its *own later page*
     (server-driven flow can't react within a page; `validate` rejects a same-page dependency).
   - Logic is the condition AST (`ref` kinds `question|url_var|variable|quota|enrichment|loop`;
     operators include `equals/not_equals/contains/is_one_of/is_not_one_of/is_answered/greater_than/
     less_than/greater_or_equal/between/regex_match`; the schema's enum is the full list).
   - Piping: `{{q:qid.answer}}`, `{{url:uid}}`, `{{var:name}}`, `{{ctx:enrichment_id}}` (alias
     `{{behavior:…}}`), and
     inside a repeat `{{loop:label}}` / `{{loop:index}}` / `{{loop:total}}`.
   - "Ask the same thing for each brand they picked" is a `repeat` on a page or question, not copies of
     it — see the `add-logic` skill and `surveys/streaming-brand-ratings.survey.json`.
   - Display-only types (no input, not required, not validated): `instructions` (title + a Markdown
     `body` — headings, bullets, links, inline `![images]()` — for a formatted task how-to),
     `video` (`media:{url, caption?}` — YouTube → privacy-mode embed, or any `<video>` URL), and
     `image` (`media:{url, alt?, caption?, max_width?, width?, height?}` — points at an image URL, with
     size controls; responsive by default). `video`/`image` require `media.url`. To capture a reaction,
     put the stimulus on one page and the question(s) about it on the next.
   - Always include: at least one `complete` action (the terminal redirect/route), `disqualify`
     actions for every screen-out with a `reason_code`, and `settings.url_variables` listing every
     URL var you reference (`uid`, `rid`, `group`, …).
   - Give it a one-line `description` (what it's for) and optional `tags`/`owner` so it reads well in
     `survey-agent list` and the dashboard.
   - For behavior-triggered questions, declare the fact in `settings.enrichments`
     (event_type + window + criteria + operation) and gate the page on it. The values come from the
     deployment's enrichment provider (see `add-logic`).
   - Attention checks are an ordinary question plus a `disqualify` rule (`settings.anti_fraud` is not
     enforced by the engine).
3. **Validate + lint:** run `survey-agent validate surveys/<slug>.survey.json`. Fix every error
   (dead branches, dangling refs, missing terminal, undeclared url_vars/enrichments) before moving on.
   (Working over MCP from chat/cowork instead of a shell? Use the `validate_survey` tool.)
3b. **Methodology review:** run `survey-agent critique surveys/<slug>.survey.json` (or the
   `critique_survey` MCP tool) and address high-severity issues — leading/double-barreled questions,
   missing opt-outs, biased scales, overlapping ranges. It's advisory (judgment, not a hard gate).
4. **Save it — the reliable way (never hand-write the `.md`).**
   - **Over MCP (chat/cowork):** call **`save_survey`** — it validates, writes
     `surveys/<slug>.survey.json`, **and** generates the readable, logic-annotated `surveys/<slug>.md`
     from the spec.
   - **With a shell:** write the `.survey.json`, then
     `survey-agent render surveys/<slug>.survey.json --write` to generate the `.md`.
   The `.md` is **engine-rendered** (frontmatter-canonical, round-trips via `compile`), so its logic
   notes — "Shown only when…", "Screen out (reason …)", quotas, piping — always match the JSON. It's
   the version a human reviews in the PR. Don't hand-write it and don't run `compile` to make it
   (`compile` goes `.md`→`.json`, the wrong direction).
5. **Summarize** the question flow, each terminal (complete, every screen-out reason, over-quota),
   quotas, and any enrichments. Hand off to `test-survey`.

## Reference
- Schema: read it with `survey-agent schema` (or the `survey://schema` MCP resource). Worked example:
  `surveys/streaming-usage.{md,survey.json}`.
- Enrichment specs: `event_type · window · criteria · operation` (examples in
  `src/survey_agent/enrichments/`).
- **No shell?** Everything above is exposed over MCP for chat/cowork: `validate_survey`,
  **`save_survey`** (writes the `.json` **and** the readable `.md`), `simulate_survey`, `list_surveys`,
  `compile_markdown_survey`, plus the `survey://schema` / `survey://catalog` resources and the
  `author_survey` prompt. See `docs/AGENTIC_MODES.md`.
