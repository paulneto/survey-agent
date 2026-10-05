---
name: edit-survey
description: Modify an existing survey-agent draft — add/remove/reword questions, change options, reorder pages, adjust quotas or theme. Triggers on "edit the survey", "change question…", "reword…", "add a question", "remove…", "tweak the survey".
---

# edit-survey

Apply a natural-language edit to an existing `surveys/<slug>.survey.json` (and its `.md`), keeping it
valid and showing a clear diff.

## Steps
1. Load the current spec. Identify the minimal change.
2. Apply it, preserving stable `id`s wherever possible — **changing a question/option `id` breaks
   logic refs, piping, quotas, and export keys.** If you must rename, update every reference.
3. Keep the same-page rule: a question gated on another answer belongs on a later page (`validate`
   rejects it otherwise). When reordering pages, a `repeat`'s `source` must stay on an earlier page
   than the repeat, and so must anything a repeated page's logic reads. Renaming a repeated question
   or a source option value changes its answer keys (`q_rate[netflix]`), so update those refs too.
4. Re-validate (`survey-agent validate …`, clean), then **regenerate the readable `.md` from the
   edited JSON** so they stay in sync: `survey-agent render surveys/<slug>.survey.json --write` (or
   re-`save_survey` over MCP, which rewrites both). Don't run `compile` here — that goes `.md`→`.json`
   (the wrong direction after a JSON edit) and would clobber your change.
5. Show a concise diff (what changed and why) and note whether logic/coverage needs re-testing.
6. If the survey was already published, remember edits create a **new version** at launch; in-flight
   respondents are unaffected. Re-run `test-survey` before `launch-survey`.

## Reference
- Schema: `survey-agent schema` (or the `survey://schema` MCP resource).
