"""Self-contained HTML reporting for survey-agent.

Reads responses — from the store, or from a quick in-memory simulation (`--simulate N`) — and
renders a single-file HTML report: completion funnel + incidence, length-of-interview, per-question
distributions, NPS, derived/segment fields, and quota burndown.

Charts use Chart.js loaded from a CDN; the **response data stays inline** in the HTML (only the
chart library is fetched from the network, never the data). Pure-Python aggregation — the engine
stays untouched; this sits beside store/ and enrich/ like any other optional surface.
"""
import json
import re
import statistics
from collections import Counter
from html import escape


CHART_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "you", "your", "i",
    "we", "it", "is", "are", "was", "that", "this", "what", "how", "why", "when", "do", "does",
    "did", "my", "our", "their", "be", "so", "but", "if", "at", "as", "me", "they"}

_RAW_TO_BUCKET = {"completed": "completed", "disqualified": "disqualified",
                  "overquota": "overquota", "user_terminated": "partial", "expired": "partial",
                  "in_progress": "partial"}


# --------------------------------------------------------------------------- collectors
def collect_via_simulation(rt, n=500, seed=7):
    """Drive N synthetic respondents and return normalized rows (raw status preserved).

    Uses the coverage gate's own respondent driver (`simulate.run_one`) with the same seed, so a
    `report --simulate N` describes exactly the respondents `simulate -n N` ran — including the same
    ESTIMATED length of interview (summed per-question timings), not a separate random number."""
    import random
    from survey_agent.simulate import run_one, _numeric_targets
    from survey_agent.engine import export
    rng = random.Random(seed)
    targets = _numeric_targets(rt.d)             # straddle numeric branches, same as the coverage gate
    qs = rt.initial_quota_state()
    rows = []
    for i in range(1, n + 1):
        sessions = []
        term, secs = run_one(rt, qs, i, rng, targets, sessions=sessions)
        s = sessions[0]
        sd = export.build_survey_data(rt, s)
        rows.append({
            "status": _RAW_TO_BUCKET.get(term["status"], "partial"),
            "reason_code": term.get("reason_code"),
            "answers": {qid: e["answer"] for qid, e in sd.items()},
            "derived": export.build_derived(rt, s),
            "group": (s.get("url_variables") or {}).get("group"),
            "loi": secs,
            "loi_estimated": True,
        })
    return rows, dict(qs)


def collect_from_store(rt, store):
    """Normalize stored export-shape responses into report rows."""
    rows = []
    for r in store.list_responses(rt.d["id"]):
        word = r.get("status")
        reason = r.get("reason_code")
        status = ("completed" if word == "Complete"
                  else "disqualified" if reason
                  else "overquota" if word == "Disqualified"
                  else "partial")
        rows.append({
            "status": status, "reason_code": reason,
            "answers": {qid: e.get("answer") for qid, e in (r.get("survey_data") or {}).items()},
            "derived": r.get("derived") or {},
            "group": (r.get("url_variables") or {}).get("group"),
            "loi": _loi_seconds(r.get("date_started"), r.get("date_submitted")),
        })
    qcounts = store.quota_counts(rt.d["id"], rt.d.get("version", 1)) if hasattr(store, "quota_counts") else {}
    return rows, {qid: {"count": qcounts.get(qid, 0), "limit": q.get("limit")}
                  for q in rt.d.get("quotas", []) for qid in [q["id"]]}


def _loi_seconds(start, end):
    from datetime import datetime
    if not start or not end:
        return None
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        return max(0, int((datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds()))
    except Exception:
        return None


def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    return xs[lo] if lo == k or lo + 1 >= len(xs) else xs[lo] + (xs[lo + 1] - xs[lo]) * (k - lo)


# --------------------------------------------------------------------------- aggregation
def build_report(rt, rows, quotas):
    statuses = Counter(r["status"] for r in rows)
    total = len(rows)
    completed = statuses.get("completed", 0)
    terminal = total - statuses.get("partial", 0)
    incidence = round(100 * completed / terminal, 1) if terminal else 0.0
    reasons = Counter(r["reason_code"] for r in rows if r["status"] == "disqualified" and r["reason_code"])
    lois = [r["loi"] for r in rows if r["status"] == "completed" and r["loi"] is not None]
    loi_estimated = any(r.get("loi_estimated") for r in rows)

    questions = []
    completed_rows = [r for r in rows if r["status"] == "completed"]
    seen = {k for r in completed_rows for k in r["answers"]}
    for qid, q in rt.questions_by_id.items():
        if q["type"] in ("video", "image", "instructions"):
            continue
        for key in rt.answer_keys(qid, seen):           # a repeated question: one chart per item
            chart = _question_chart(q, key, completed_rows)
            if chart:
                if key != qid:
                    chart["title"] = rt.instance_title(key, q.get("title", qid))
                chart["title"] = display_title(chart["title"])
                questions.append(chart)

    derived = []
    for h in rt.d.get("hidden", []):
        vals = Counter(str(r["derived"].get(h["id"])) for r in completed_rows if r["derived"].get(h["id"]) is not None)
        if vals:
            derived.append({"id": h["id"], "labels": list(vals.keys()), "counts": list(vals.values())})
    for spec in rt.d.get("settings", {}).get("enrichments", []):
        if isinstance(spec, dict) and spec.get("source") == "llm":
            vals = Counter(str(r["derived"].get(spec["id"])) for r in completed_rows if r["derived"].get(spec["id"]) is not None)
            if vals:
                derived.append({"id": spec["id"] + " (LLM)", "labels": list(vals.keys()), "counts": list(vals.values())})

    quota_rows = [{"id": qid, "count": c.get("count", 0), "limit": c.get("limit")}
                  for qid, c in (quotas or {}).items()]

    return {
        "title": rt.d.get("title", rt.d["id"]),
        "survey_id": rt.d["id"],
        "version": rt.d.get("version", 1),
        "n": total,
        "funnel": {"labels": ["completed", "disqualified", "overquota", "partial"],
                   "counts": [statuses.get("completed", 0), statuses.get("disqualified", 0),
                              statuses.get("overquota", 0), statuses.get("partial", 0)]},
        "incidence": incidence,
        "reasons": {"labels": list(reasons.keys()), "counts": list(reasons.values())},
        # LOI = median (and p90) over COMPLETED responses. Seconds are kept for compatibility;
        # *_min is what every surface displays (simulate, report, dashboard) so the units agree.
        "loi": {"median": round(statistics.median(lois)) if lois else None,
                "p90": round(_pct(lois, 90)) if lois else None, "n": len(lois),
                "median_min": round(statistics.median(lois) / 60, 1) if lois else None,
                "p90_min": round(_pct(lois, 90) / 60, 1) if lois else None,
                "estimated": loi_estimated},
        "questions": questions,
        "derived": derived,
        "quotas": quota_rows,
    }


def _question_chart(q, qid, rows):
    t = q["type"]
    answers = [r["answers"].get(qid) for r in rows if r["answers"].get(qid) not in (None, "")]
    if not answers:
        return None
    base = {"id": qid, "title": q.get("title", qid), "type": t, "kind": "bar"}
    if t in ("radio", "dropdown", "rating"):
        c = Counter(str(a) for a in answers)
        return {**base, "labels": list(c.keys()), "counts": list(c.values())}
    if t in ("checkbox", "dropdown_multi"):
        c = Counter()
        for a in answers:
            for v in (a if isinstance(a, list) else [a]):
                c[str(v)] += 1
        return {**base, "labels": list(c.keys()), "counts": list(c.values()), "note": "multi-select"}
    if t == "nps":
        nums = [int(a) for a in answers if str(a).lstrip("-").isdigit()]
        if not nums:
            return None
        prom = sum(1 for x in nums if x >= 9)
        det = sum(1 for x in nums if x <= 6)
        nps = round(100 * (prom - det) / len(nums))
        c = Counter(nums)
        return {**base, "labels": [str(i) for i in range(0, 11)],
                "counts": [c.get(i, 0) for i in range(0, 11)], "note": f"NPS = {nps}"}
    if t == "number":
        nums = [float(a) for a in answers if _isnum(a)]
        if not nums:
            return None
        lo, hi = min(nums), max(nums)
        bins = 8 if hi > lo else 1
        width = (hi - lo) / bins or 1
        edges = [lo + i * width for i in range(bins + 1)]
        counts = [0] * bins
        for x in nums:
            k = min(bins - 1, int((x - lo) / width)) if width else 0
            counts[k] += 1
        labels = [f"{edges[i]:.0f}–{edges[i+1]:.0f}" for i in range(bins)]
        return {**base, "labels": labels, "counts": counts, "note": f"median {round(statistics.median(nums))}"}
    if t == "textarea" or t == "text":
        words = Counter()
        for a in answers:
            for w in re.findall(r"[a-zA-Z']{3,}", str(a).lower()):
                if w not in _STOP:
                    words[w] += 1
        top = words.most_common(12)
        if not top:
            return {**base, "kind": "count", "answered": len(answers)}
        return {**base, "labels": [w for w, _ in top], "counts": [n for _, n in top],
                "note": f"{len(answers)} open-text answers · top terms"}
    return None


def _isnum(a):
    try:
        float(a)
        return True
    except (TypeError, ValueError):
        return False


def _loi_min(loi, key):
    """LOI in minutes (1 dp) from a report's loi block — tolerant of reports built before *_min existed."""
    v = loi.get(f"{key}_min")
    if v is None and loi.get(key) is not None:
        v = round(loi[key] / 60, 1)
    return v


_TOKEN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def display_title(text):
    """A question title as an operator surface (report, insights, export headers) shows it: merge
    tokens are never resolved against any respondent's data here — each `{{kind:ref|fallback}}` becomes
    its fallback, or a neutral `[…]` placeholder when it has none. Consistent across report + insights."""
    if not text:
        return text

    def sub(m):
        _, bar, fallback = m.group(1).partition("|")
        fallback = fallback.strip()
        return fallback if bar and fallback else "[…]"
    return re.sub(r"[ \t]{2,}", " ", _TOKEN.sub(sub, str(text))).strip()


# --------------------------------------------------------------------------- HTML
def render_html(report, insights_html=""):
    data = json.dumps(report)
    loi = report["loi"]
    _est = " · estimated from question timings (simulation)" if loi.get("estimated") else ""
    loi_txt = (f'median {_loi_min(loi, "median")} min · p90 {_loi_min(loi, "p90")} min '
               f'(completes, n={loi["n"]}{_est})'
               if loi["median"] is not None else "no completed responses with timing")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache"><meta http-equiv="Expires" content="0">
<title>{escape(report['title'])} — report</title>
<script src="{CHART_CDN}"></script>
<style>
  :root {{ --bg:#ece0c8; --card:#f7efda; --ink:#3b3022; --muted:#8a7a5e; --line:#d9c7a3; --accent:#9a5b2d; }}
  body {{ background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; margin:0; padding:32px; }}
  .wrap {{ max-width:920px; margin:0 auto; }}
  h1 {{ margin:0 0 4px; }} .sub {{ color:var(--muted); margin:0 0 24px; }}
  .kpis {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; }}
  .kpi {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 18px; min-width:140px; }}
  .kpi b {{ font-size:26px; display:block; }} .kpi span {{ color:var(--muted); font-size:13px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin-bottom:18px; }}
  .card h3 {{ margin:0 0 2px; font-size:15px; }} .card .note {{ color:var(--muted); font-size:12px; margin:0 0 10px; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
  @media(max-width:680px){{ .grid {{ grid-template-columns:1fr; }} }}
  footer {{ color:var(--muted); font-size:12px; margin-top:24px; }}
</style></head><body><div class="wrap">
<h1>{escape(report['title'])}</h1>
<p class="sub">survey <code>{escape(report['survey_id'])}</code> v{report['version']} · {report['n']} responses</p>
<div class="kpis">
  <div class="kpi"><b>{report['n']}</b><span>responses</span></div>
  <div class="kpi"><b>{report['funnel']['counts'][0]}</b><span>completed</span></div>
  <div class="kpi"><b>{report['incidence']}%</b><span>incidence</span></div>
  <div class="kpi"><b>{(str(_loi_min(loi, "median")) + ' min') if loi['median'] is not None else '—'}</b><span>median LOI{' (est.)' if loi.get('estimated') else ''}</span></div>
</div>
{insights_html}
<div class="grid">
  <div class="card"><h3>Outcome funnel</h3><p class="note">LOI: {loi_txt}</p><canvas id="c_funnel"></canvas></div>
  <div class="card"><h3>Screen-out reasons</h3><p class="note">why people dropped</p><canvas id="c_reasons"></canvas></div>
</div>
<div id="questions"></div>
<div id="derived"></div>
<div class="card"><h3>Quota burndown</h3><div id="quotas"></div></div>
<footer>Generated by survey-agent · data is embedded in this file; only the chart library loads from a CDN.</footer>
</div>
<script>
const R = {data};
const COLOR = "#9a5b2d", GRID = "rgba(59,48,34,.08)";
function bar(id, labels, counts, horizontal) {{
  const el = document.getElementById(id); if(!el) return;
  new Chart(el, {{ type:"bar", data:{{ labels, datasets:[{{ data:counts, backgroundColor:COLOR }}] }},
    options:{{ indexAxis: horizontal?"y":"x", plugins:{{legend:{{display:false}}}},
      scales:{{ x:{{grid:{{color:GRID}}}}, y:{{grid:{{color:GRID}},beginAtZero:true}} }} }} }});
}}
bar("c_funnel", R.funnel.labels, R.funnel.counts, true);
bar("c_reasons", R.reasons.labels.length?R.reasons.labels:["(none)"], R.reasons.counts.length?R.reasons.counts:[0], true);
const qroot = document.getElementById("questions");
R.questions.forEach((q,i) => {{
  const id = "q"+i;
  const card = document.createElement("div"); card.className="card";
  card.innerHTML = `<h3>${{escapeHtml(q.title)}}</h3><p class="note">${{q.type}}${{q.note?" · "+q.note:""}}</p>`+
                   (q.kind==="count" ? `<p>${{q.answered}} answered</p>` : `<canvas id="${{id}}"></canvas>`);
  qroot.appendChild(card);
  if(q.kind!=="count") bar(id, q.labels, q.counts, q.labels.length>6);
}});
const droot = document.getElementById("derived");
R.derived.forEach((d,i) => {{
  const id="d"+i; const card=document.createElement("div"); card.className="card";
  card.innerHTML = `<h3>${{escapeHtml(d.id)}}</h3><p class="note">derived / hidden field</p><canvas id="${{id}}"></canvas>`;
  droot.appendChild(card); bar(id, d.labels, d.counts, true);
}});
const qz = document.getElementById("quotas");
qz.innerHTML = R.quotas.length ? R.quotas.map(q => {{
  const lim = q.limit==null?"∞":q.limit; const pct = q.limit?Math.min(100,Math.round(100*q.count/q.limit)):0;
  return `<div style="margin:8px 0"><div style="display:flex;justify-content:space-between"><span>${{escapeHtml(q.id)}}</span><span>${{q.count}}/${{lim}}</span></div>`+
         `<div style="background:#e2d6bd;border-radius:6px;height:8px"><div style="width:${{pct}}%;background:${{COLOR}};height:8px;border-radius:6px"></div></div></div>`;
}}).join("") : "<p class='note'>no quotas</p>";
function escapeHtml(s){{ return String(s).replace(/[&<>]/g, c=>({{"&":"&amp;","<":"&lt;",">":"&gt;"}}[c])); }}
</script></body></html>"""


# --------------------------------------------------------------------------- entry
def generate(rt, store=None, simulate_n=0, seed=7, with_insights=False, llm=None, return_report=False):
    if simulate_n:
        rows, quotas = collect_via_simulation(rt, simulate_n, seed)
    else:
        rows, quotas = collect_from_store(rt, store)
    report = build_report(rt, rows, quotas)
    insights_html = ""
    if with_insights:
        from survey_agent import insights as insights_mod
        insights_html = insights_mod.render_insights_html(
            insights_mod.generate_insights(rt, rows, report, llm=llm))
    html = render_html(report, insights_html)
    return (html, report) if return_report else html
