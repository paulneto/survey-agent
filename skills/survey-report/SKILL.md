---
name: survey-report
description: Pull fielding stats and analytics for a survey-agent survey — completes, screen-outs and over-quota, incidence, length (LOI), screen-out reasons, quota burndown, per-question distributions, an AI read-out, and a CSV/Parquet export for cross-tabs. Triggers on "how is the survey doing", "survey report", "incidence/completes", "summarize the results", "cross-tab question X by Y", "export the responses".
---

# survey-report

Report on a live or closed survey from the responses in the configured store (`SURVEY_STORE`), or
preview a report from synthetic respondents before there's real data.

## What each tool gives you

| Need | CLI | MCP tool |
|---|---|---|
| Fielding stats + question charts | `survey-agent report <survey> [--open]` | `survey_report` |
| AI read-out of the results | `survey-agent insights <survey>` (or `report --insights`) | `survey_insights` |
| Row-level data for your own analysis | `survey-agent export <survey> [--format csv\|parquet] [--out FILE]` | `export_survey` |
| Every survey on one page | `survey-agent dashboard [--live] [--open]` | `build_dashboard` |

All of them accept `--simulate N` (MCP: `simulate=N`) to work from N synthetic respondents instead of
the store, which is handy for a pre-launch preview.

## Steps

1. **Fielding stats:** `survey-agent report <survey.json> --open` writes a self-contained HTML report
   (charts via Chart.js) and opens it. It contains:
   - **N** and the **outcome funnel**: completed / disqualified / overquota / partial.
   - **Incidence**: completes as a % of respondents who reached a terminal (partials excluded).
   - **LOI**: median and 90th-percentile time from start to submit, over completes.
   - **Screen-out reasons**: counts per `reason_code`.
   - **Per-question distributions** over completes (bar charts; multi-select; NPS with the score;
     numeric questions). A repeated question gets one chart per item.
   - **Derived fields** (hidden/calculated values and any `source: llm` analysis).
   - **Quota burndown**: count vs. limit per quota.
   The `survey_report` MCP tool returns the same data as a dict. Always state N when you quote numbers.
2. **Flag drift** from the brief: is incidence or LOI far off what the author estimated? Are the
   screen-out reasons the ones you expected?
3. **Read-out:** `survey-agent insights <survey.json>` (or `report --insights`) adds an analyst
   read-out: headline, key findings, open-text themes, watch-outs and suggested next steps. With a
   model key it's written by an LLM and **sends a sample of open-text answers to that model**; with no
   key a deterministic offline read-out is produced instead. Tell the human which one they got.
4. **Cross-tabs:** the report shows one-way distributions only. For a cross-tab ("hours saved by
   role"), export the rows and tabulate them: `survey-agent export <survey.json> --out responses.csv`
   (or `--format parquet`, which needs the `[data]` extra). One row per response; columns are the id,
   status, timestamps, `url.<var>` for each URL variable, one column per question, and
   `derived.<field>`. Free text is included; file-upload answers are references, not bytes.
5. **Portfolio view:** `survey-agent dashboard --live --open` lists every survey with its drop-in URL
   and live completes / incidence / quota from the store (`--base-url` sets the link base).

## Notes

- The CLI reads **your local configuration's** store. To report on a hosted deployment, point the CLI
  at the same store (e.g. `SURVEY_STORE=mongo` with its connection settings) or at the API gateway
  (`SURVEY_STORE=http`).
- `report` and `dashboard` count the responses in the store. They don't see your panel's or CRM's
  own records, so reconcile against your own system if you need to.
- The response shape is stable (`url_variables` + `survey_data` + `derived`), so the reporter, the
  export and any external analysis tooling read the same thing.
- A repeated question has one `survey_data` entry per item, keyed `qid[item]` (`q_sat[netflix]`), with
  an extra `repeat: {source, value, label}`. The report and CSV export have one chart / column per
  item, in source-option order. Cross-tab or compare items by those keys.
