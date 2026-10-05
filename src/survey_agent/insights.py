"""Auto-insight — an LLM analyst's read-out of a survey's results at fielding end. It sits on top of
the aggregated stats `report.build_report()` already computes, plus a sample of open-text answers,
and returns a structured analysis: headline, key findings, open-end themes, watch-outs, and suggested
follow-up questions. Turns *collection* into *analysis*.

Opt-in + offline-safe: with no key a deterministic stub produces a mechanical-but-useful read-out;
with a key it's narrative. Sending open-text to an LLM is respondent data — disclosed (the rendered
block says so) and, in the runtime, recorded in the llm_audit like other LLM-on-answers features.
"""
from html import escape


def open_ends(rt, rows, per_q=30):
    """{question_title: [answer, ...]} sampled from completed rows, for the LLM to theme."""
    out = {}
    seen = {k for r in rows for k in r["answers"]}
    for qid, q in rt.questions_by_id.items():
        if q.get("type") not in ("textarea", "text"):
            continue
        for key in rt.answer_keys(qid, seen):           # a repeated question: one entry per item
            vals = [str(r["answers"].get(key)) for r in rows
                    if r["status"] == "completed" and r["answers"].get(key)]
            if vals:
                from survey_agent.report import display_title
                name = display_title(q.get("title") or qid)[:60]
                if key != qid:                          # keep the item visible however long the title
                    full = display_title(rt.instance_title(key, q.get("title") or qid))
                    label = (rt.instance_item(key) or {}).get("label", key)
                    name = full if len(full) <= 60 else f"{full[:45]}… [{label}]"
                out[name if name not in out else f"{name} ({key})"] = vals[:per_q]
    return out


def generate_insights(rt, rows, report, llm=None):
    import survey_agent.ai as ai
    llm = llm or ai.get_llm()
    try:
        return llm.insights(report, open_ends(rt, rows))
    except Exception:
        return ai._stub_insights(report, None)        # never let analysis break the report


def render_insights_html(ins):
    def ul(xs):
        return ("<ul>" + "".join(f"<li>{escape(str(x))}</li>" for x in xs) + "</ul>") if xs else ""
    themes = "".join(f"<li><b>{escape(str(t.get('theme', '')))}</b> — {escape(str(t.get('note', '')))}</li>"
                     for t in ins.get("themes", []))
    block = (f'<div class="card" style="border-left:4px solid var(--accent)">'
             f'<h3>🤖 AI insights <span class="note">· {escape(str(ins.get("engine", "?")))} · generated '
             f'from responses incl. open-text (opt-in)</span></h3>'
             f'<p style="font-size:16px;margin:.2em 0"><b>{escape(str(ins.get("headline", "")))}</b></p>'
             f'<p class="note">Key findings</p>{ul(ins.get("findings", []))}')
    if themes:
        block += f'<p class="note">Open-end themes</p><ul>{themes}</ul>'
    if ins.get("watchouts"):
        block += f'<p class="note">Watch-outs</p>{ul(ins["watchouts"])}'
    if ins.get("next_steps"):
        block += f'<p class="note">Suggested follow-ups</p>{ul(ins["next_steps"])}'
    return block + "</div>"


def to_text(ins):
    out = [f"\n🤖 AI insights  ({ins.get('engine', '?')})", "", ins.get("headline", ""), "", "Key findings:"]
    out += [f"  • {f}" for f in ins.get("findings", [])]
    if ins.get("themes"):
        out += ["", "Open-end themes:"] + [f"  • {t.get('theme', '')} — {t.get('note', '')}" for t in ins["themes"]]
    if ins.get("watchouts"):
        out += ["", "Watch-outs:"] + [f"  • {w}" for w in ins["watchouts"]]
    if ins.get("next_steps"):
        out += ["", "Suggested follow-ups:"] + [f"  • {s}" for s in ins["next_steps"]]
    return "\n".join(out)
