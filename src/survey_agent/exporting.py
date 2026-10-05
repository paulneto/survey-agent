"""Tabular export — flatten responses into a CSV (always) or Parquet (optional) grid for analysts
and spreadsheets. One row per response; columns are id/status/timestamps + url.<var> + each
question + derived.<field>. Reads from the store or a quick in-memory simulation.

CSV uses the stdlib (zero deps). Parquet needs pandas+pyarrow (`pip install 'survey-agent[data]'`)."""
import csv
import io

from survey_agent.engine import export as export_mod


def collect_responses(rt, store=None, simulate_n=0, seed=7):
    if not simulate_n:
        return list(store.list_responses(rt.d["id"]))
    import random
    from survey_agent.simulate import answer_question
    rng = random.Random(seed)
    qs = rt.initial_quota_state()
    out = []
    for i in range(1, simulate_n + 1):
        uv = {rt.respondent_var: str(i), rt.response_var: str(5000 + i % 100),
              "group": rng.choice(["A", "B"]), "lang": "en"}
        s = rt.new_session(f"sim:{i}", uv)
        step = rt.start(s, qs)
        guard = 0
        while "page" in step and guard < rt.max_steps:
            guard += 1
            page = step["page"]
            ans = {q["id"]: answer_question(q, rng) for q in page["questions"]}
            step = rt.submit(s, page["page_id"], ans, qs)
        if step["terminal"]["status"] == "completed":
            for qid in rt.quota_buckets(s):
                c = qs.get(qid)
                if c and (c["limit"] is None or c["count"] < c["limit"]):
                    c["count"] += 1
        out.append(export_mod.response_from_session(rt, s))
    return out


def flatten(rt, responses):
    """Return (columns, rows[dict]). Multi-select answers are joined with '|'."""
    seen = {k for r in responses for k in (r.get("survey_data") or {})}
    qids = [key for qid, q in rt.questions_by_id.items() if q["type"] not in ("video", "image", "instructions")
            for key in rt.answer_keys(qid, seen)]       # a repeated question: one column per item
    uvkeys = sorted({k for r in responses for k in (r.get("url_variables") or {})})
    dkeys = sorted({k for r in responses for k in (r.get("derived") or {})})
    cols = (["id", "status", "reason_code", "date_started", "date_submitted"]
            + [f"url.{k}" for k in uvkeys] + qids + [f"derived.{k}" for k in dkeys])
    rows = []
    for r in responses:
        sd, uv, dv = r.get("survey_data") or {}, r.get("url_variables") or {}, r.get("derived") or {}
        row = {"id": r.get("id"), "status": r.get("status"), "reason_code": r.get("reason_code"),
               "date_started": r.get("date_started"), "date_submitted": r.get("date_submitted")}
        for k in uvkeys:
            row[f"url.{k}"] = uv.get(k, "")
        for qid in qids:
            a = (sd.get(qid) or {}).get("answer")
            if isinstance(a, dict):       # grid: row=choice (multi-choice rows joined with +)
                row[qid] = "; ".join(f"{k}={'+'.join(map(str, v)) if isinstance(v, list) else v}"
                                     for k, v in a.items())
            elif isinstance(a, list):
                row[qid] = "|".join(map(str, a))
            else:
                row[qid] = "" if a is None else a
        for k in dkeys:
            row[f"derived.{k}"] = dv.get(k, "")
        rows.append(row)
    return cols, rows


def to_csv(cols, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow(row)
    return buf.getvalue()


def to_parquet(cols, rows, path):
    try:
        import pandas as pd
    except ImportError:
        raise SystemExit("Parquet export needs pandas+pyarrow:  pip install 'survey-agent[data]'")
    pd.DataFrame(_parquet_rows(cols, rows), columns=cols).to_parquet(path, index=False)


def _parquet_rows(cols, rows):
    """Make rows Arrow-safe. The CSV grid writes a blank optional answer as '' — fine in a CSV, but in
    Parquet '' next to numbers is a type clash (ArrowInvalid). Blanks become nulls, and a column whose
    non-null values mix types (e.g. numbers and text) is written as text so it never fails to encode."""
    out = [{c: (None if row.get(c) in ("", None) else row.get(c)) for c in cols} for row in rows]
    for c in cols:
        vals = [r[c] for r in out if r[c] is not None]
        if not vals:
            continue
        numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
        boolean = all(isinstance(v, bool) for v in vals)
        textual = all(isinstance(v, str) for v in vals)
        if not (numeric or boolean or textual):
            for r in out:
                if r[c] is not None:
                    r[c] = str(r[c])
    return out
