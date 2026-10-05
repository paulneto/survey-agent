"""Build the parity response object from a runtime session.

Downstream tooling (your DSAR/data-export pipeline) reads responses as
{id, date_started, date_submitted, status, url_variables, survey_data{qid:{id,question,answer}}}.
A repeated question has one survey_data entry per item, keyed `qid[item]`, carrying an extra
`repeat: {source, value, label}`.
Optional keys are added only when present: `derived` (always), `client` (network metadata —
ip/user_agent/fingerprint, captured by the runtime only when SURVEY_CAPTURE_CLIENT_META is on),
`llm_audit`, `probes`. This module produces exactly that shape from a session, so the export API
and the flat-file store can both emit it. Pure-python; no storage dependency.
"""
from .runtime import response_id

# runtime status -> standard status word (downstream tools switch on this string)
STATUS_WORD = {
    "completed": "Complete",
    "disqualified": "Disqualified",
    "overquota": "Disqualified",
    "user_terminated": "Partial",
    "expired": "Partial",
    "in_progress": "Partial",
}


def build_survey_data(rt, session):
    """{question_id: {id, question, answer}} with display labels (multi-select -> list).
    The question text is the piped/resolved title (what the respondent actually saw)."""
    from .ast_eval import EvalContext
    from . import piping
    ctx = EvalContext(answers=session.get("answers", {}), url_vars=session.get("url_vars", {}),
                      variables=session.get("variables", {}), enrichments=session.get("enrichments", {}))
    out = {}
    for qid, val in session.get("answers", {}).items():
        q = rt.questions_by_id.get(qid, {})
        item = rt.instance_item(qid)          # a repeated question's answer for one item: q_rate[netflix]
        if item:
            ictx = ctx.with_loop(rt.instance_loop(qid, session))
            question_text = piping.resolve_text(q.get("title", ""), ictx, rt.label_lut)
        else:
            question_text = piping.resolve_text(q.get("title", ""), ctx, rt.label_lut)
        # Single source of truth for value→label (rt.label_lut also carries piped-option labels
        # inherited from the pipe_from question), so an `options_source` answer exports its label.
        opts = dict(rt.label_lut.get(qid, {}))
        if q.get("type") == "grid" and isinstance(val, dict):     # {row_id: col_value(s)} -> {row_label: col_label(s)}
            rowlbl = {r["id"]: r.get("label", r["id"]) for r in q.get("rows", [])}
            def _lbl(cv):
                return [opts.get(str(x), x) for x in cv] if isinstance(cv, list) else opts.get(str(cv), cv)
            answer = {rowlbl.get(rid, rid): _lbl(cv) for rid, cv in val.items()}
        elif opts:
            answer = [opts.get(str(v), v) for v in val] if isinstance(val, list) else opts.get(str(val), val)
        else:
            answer = val
        entry = {"id": qid, "question": question_text, "answer": answer}
        if item:
            entry["repeat"] = item            # {source, value, label} — which item this answer is about
        if opts and isinstance(val, list):
            entry["options"] = {str(v): opts.get(str(v), v) for v in val}
        out[qid] = entry
    return out


def build_derived(rt, session):
    """Hidden/derived values stored on the response: calc-based `hidden` fields + LLM-analysis
    context (which can also be piped to screen). These are the 'hidden values' of the response."""
    out = {}
    variables = session.get("variables", {})
    enrichments = session.get("enrichments", {})
    for h in rt.d.get("hidden", []):
        out[h["id"]] = variables.get(h["id"])
    for s in rt.d.get("settings", {}).get("enrichments", []):
        if isinstance(s, dict) and s.get("source") == "llm":
            out[s["id"]] = enrichments.get(s["id"])
    return out


def response_from_session(rt, session, date_started=None, date_submitted=None):
    resp = {
        "id": response_id(session),
        "survey_id": session.get("survey_id"),
        "date_started": date_started,
        "date_submitted": date_submitted,
        "status": STATUS_WORD.get(session.get("status"), session.get("status")),
        "reason_code": session.get("reason_code"),
        "url_variables": dict(session.get("url_vars", {})),
        "survey_data": build_survey_data(rt, session),
        "derived": build_derived(rt, session),
    }
    if session.get("client"):
        resp["client"] = session["client"]        # network metadata (ip/ua/fingerprint) — fraud signals
    if session.get("llm_audit"):
        resp["llm_audit"] = session["llm_audit"]  # opt-in LLM analysis is recorded on the response
    if session.get("probes"):
        resp["probes"] = session["probes"]        # adaptive-probe follow-ups (question + reply), audited
    return resp
