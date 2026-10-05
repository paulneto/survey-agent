---
name: test-survey
description: Simulate respondents through a survey-agent survey to validate logic, routing, quotas, and terminal passback events before launch. Triggers on "test the survey", "simulate respondents", "check the logic/routing", "does this screener work".
---

# test-survey

The pre-launch gate. Drives synthetic respondents through every branch and checks that each path
reaches the right terminal and maps to a terminal event (the passback your integration would send).
**`launch-survey` must not run until this passes.**

## Steps

1. **Validate first:** `survey-agent validate surveys/<slug>.survey.json` must be clean.
2. **Simulate:** `survey-agent simulate surveys/<slug>.survey.json --n 500`
   - Confirm the gate line:
     `✅ coverage gate passed: complete + every declared screen-out reachable; all callbacks mapped.`
     On failure it lists the problems (an unreachable screen-out, a stuck path, …). Fix and re-run.
   - Read the distribution: terminal statuses, screen-out reasons, the terminal events that would
     fire, final quota counters and the estimated length of interview (LOI). Are screen-out reasons
     firing as intended? Is incidence (% complete) near the brief's estimate? If "Never used" never
     disqualifies, the logic is wrong. Simulated answers are random, so incidence here tests
     reachability, not the real audience.
3. **Exercise quotas:** `survey-agent simulate … --quota N` to confirm over-quota terminates
   correctly once a cell fills.
3b. **Persona pre-test (qualitative):** `survey-agent pretest surveys/<slug>.survey.json --n 6`
   drives a few LLM-persona respondents (role-played people, not coin flips) to sanity-check that
   *realistic* answers produce a sensible incidence and the right screen-outs. `simulate` proves every
   path is **reachable**; `pretest` previews what a **real audience** might do. Keep N small: with a
   model key each persona makes about one request per question. The engine stays authoritative either
   way.
3c. **Repeats:** if the survey uses `repeat`, check the simulated incidence of per-item follow-ups
   (e.g. a `q_sat[@]`-gated page) is plausible, and that the `.md` (`survey-agent render`) shows each
   🔁 line looping over the source you meant. A `by: value` repeat runs up to its `max`.
4. **Check enrichment-gated paths:** if the survey uses `{{ctx:…}}` / `{{behavior:…}}` or enrichment
   logic, verify those pages appear only when the fact is present. The simulator runs without
   enrichment values, so those pages should be *absent* there; confirm they're gated, then spot-check
   with the demo.
5. **Live click-through (optional but recommended):** `survey-agent demo --survey surveys/<slug>.survey.json`
   starts the runtime on :8000 (serving every lifecycle state, so drafts work) and a mock receiver on
   :9000. Walk the key paths in the browser and watch the receiver's terminal for the
   `started` / `completed` / `terminated` events. Responses are written to `./survey_data`.
6. **Report** the path coverage, incidence, LOI and any logic gaps. Block launch on failures.

## Reference

- Over MCP (no shell): the `simulate_survey` tool runs the same coverage gate; `pretest_survey` runs
  the persona pre-test.
