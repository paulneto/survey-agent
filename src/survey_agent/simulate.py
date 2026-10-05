"""Drive N synthetic respondents through a survey to validate logic, routing, quotas, and
the terminal/passback each path produces. Used by the `test-survey` skill as the pre-launch
gate, and runnable directly.

    survey-agent simulate surveys/streaming-usage.survey.json --n 500
    survey-agent simulate surveys/streaming-usage.survey.json --n 500 --quota 5
"""
import argparse
import json
import os
import random
import statistics
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey_agent.engine.runtime import SurveyRuntime, STATUS_COMPLETED, STATUS_DISQUALIFIED
from survey_agent.engine import piping
from survey_agent.engine import terminal as terminal_map


_NUMERIC_OPS = {"greater_than", "less_than", "greater_or_equal", "less_or_equal",
                "between", "equals", "not_equals"}


def _iter_conditions(node):
    """Yield every leaf condition ({ref, operator, value}) inside a logic tree (recursing through
    nested and/or groups — the only group form the engine and schema support)."""
    if not isinstance(node, dict):
        return
    for child in node.get("conditions", []):
        yield from _iter_conditions(child)
    if "ref" in node and "operator" in node:
        yield node


def _numeric_targets(spec):
    """Boundary values that straddle every numeric condition on a question, so the coverage run
    *deterministically* exercises narrow numeric branches (e.g. `age less_than 18` over a 16–99
    range) instead of relying on a uniform random draw to stumble into a ~2% window. Values outside
    a question's own validation range are dropped downstream — a branch needing an out-of-range
    answer is genuinely unreachable and stays flagged as a dead branch."""
    s = spec.get("survey", spec)
    targets, sources = {}, [a.get("when") for a in s.get("actions", [])]
    for p in s.get("pages", []):
        sources.append(p.get("display_logic"))
        for q in p.get("questions", []):
            sources.append(q.get("display_logic"))
    for root in sources:
        for cond in _iter_conditions(root):
            ref = cond.get("ref") or {}
            op, val, qid = cond.get("operator"), cond.get("value"), ref.get("id")
            if ref.get("kind") != "question" or op not in _NUMERIC_OPS or qid is None:
                continue
            bucket = targets.setdefault(qid, set())
            if op == "between" and isinstance(val, (list, tuple)) and len(val) == 2:
                lo, hi = val
                bucket.update(v for v in (lo - 1, lo, hi, hi + 1) if isinstance(v, (int, float)))
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                bucket.update((val - 1, val, val + 1))   # straddle the threshold (covers </<=/>/>=/==/!=)
    return {qid: sorted(vals) for qid, vals in targets.items()}


def answer_question(q, rng, targets=None):
    t = q["type"]
    opts = [o["value"] for o in q.get("options", [])] or list(q.get("scale_points", []))
    if t in ("radio", "dropdown", "rating", "nps"):
        return rng.choice(opts) if opts else ""
    if t in ("checkbox", "dropdown_multi"):
        k = rng.randint(1, max(1, len(opts)))
        return rng.sample(opts, k) if opts else []
    if t == "ranking":
        shuffled = list(opts)
        rng.shuffle(shuffled)
        return shuffled
    if t == "grid":
        cols = [o["value"] for o in q.get("options", [])]
        if not cols:
            return {}
        return {r["id"]: (rng.sample(cols, rng.randint(1, len(cols))) if q.get("cell_type") == "checkbox"
                          else rng.choice(cols))
                for r in q.get("rows", [])}
    if t == "number":
        v = q.get("validation", {})
        lo, hi = int(v.get("min", 18)), int(v.get("max", 65))
        base = rng.randint(lo, hi)
        tg = [int(x) for x in (targets or {}).get(q["id"], []) if lo <= x <= hi]
        return rng.choice([base] + tg) if tg else base   # bias toward branch boundaries for coverage
    if t == "date":
        return "2026-01-01"
    if t in ("text", "textarea"):
        v = q.get("validation", {})
        if not q.get("required"):
            return ""                              # optional free-text: realistic to skip, and avoids
                                                   # invalid-length re-serve loops on bounded fields
        if v.get("format") == "email":
            return "user@example.com"
        minl, maxl = v.get("min_length") or 0, v.get("max_length")
        s = "synthetic text response"
        if len(s) < minl:                          # pad up to a required minimum length
            s = (s + " " + "x" * minl)[:minl]
        if maxl is not None:                       # then respect a maximum length
            s = s[:maxl]
        return s
    return "synthetic text"


# coarse per-question time (seconds) — enough to flag a survey that's too long, not a stopwatch
_QUESTION_SECONDS = {
    "radio": 7, "dropdown": 7, "rating": 7, "nps": 8, "number": 8, "email": 12, "date": 10,
    "checkbox": 12, "dropdown_multi": 12, "ranking": 18, "text": 15, "textarea": 35,
}


def _question_time(q):
    t = q.get("type")
    if t == "instructions":
        words = len((q.get("body") or q.get("title") or "").split())
        return max(2.0, words / 3.5)                       # ~210 wpm reading
    if t == "video":
        return float((q.get("media") or {}).get("seconds", 20))
    if t == "image":
        return 5.0
    if t == "grid":
        return 6.0 * max(1, len(q.get("rows", [])))
    return float(_QUESTION_SECONDS.get(t, 8))


# share of optional (non-display) questions a synthetic respondent leaves blank, so the paths where an
# optional answer is missing (and anything piped from it) get exercised
SKIP_OPTIONAL = 0.2


def run_one(rt, quota_state, uid, rng, targets=None, blanks=None, seen=None, sessions=None):
    """One synthetic respondent. `blanks` / `seen` (Counters), when given, collect merge tokens that
    rendered blank (keyed (page, where, token)) and how many respondents saw each page. `sessions`
    (a list), when given, receives the finished session — so the report/export simulations reuse the
    exact same respondents (and LOI estimate) as the coverage gate.

    Returns (terminal, seconds): `seconds` is the ESTIMATED length of interview — the sum of
    per-question reading/answer times (`_question_time`) over every question shown. It is the one LOI
    definition for simulated respondents, used by `simulate`, `report --simulate` and `author`."""
    # key url_vars by the survey's configured identity vars (defaults uid/rid) so simulation
    # works regardless of what a deployment names its identity parameters
    url_vars = {rt.respondent_var: str(uid), rt.response_var: str(5000 + uid % 100),
                "group": rng.choice(["demo", "vip"]), "lang": "en"}
    session = rt.new_session(f"sim:{uid}", url_vars)
    if sessions is not None:
        sessions.append(session)
    step = rt.start(session, quota_state)
    guard, seconds = 0, 0.0
    while "page" in step and guard < rt.max_steps:
        guard += 1
        page = step["page"]
        if blanks is not None:
            seen[page["page_id"]] += 1
            for where, tok in rt.blank_tokens(session):
                if piping.split_token(tok[2:-2])[0] in ("q", "var", "loop"):
                    blanks[(page["page_id"], where, tok)] += 1
        answers = {}
        for q in page["questions"]:
            seconds += _question_time(q)                   # accumulate estimated length-of-interview
            if not q.get("required") and q["type"] not in ("instructions", "video", "image") \
                    and rng.random() < SKIP_OPTIONAL:
                continue                                   # left blank, as real respondents do
            answers[q["id"]] = answer_question(q, rng, targets)
        step = rt.submit(session, page["page_id"], answers, quota_state)
    if "terminal" not in step:
        # Guard exhausted with a page still showing: a question here is unanswerable (e.g. a required
        # field whose validation no answer can satisfy), so the survey can never complete. Surface it
        # as a coverage problem instead of crashing the gate with a KeyError.
        return {"status": "stuck", "page_id": step.get("page", {}).get("page_id")}, seconds
    terminal = step["terminal"]
    if terminal["status"] == "completed":
        for qid in rt.quota_buckets(session):
            c = quota_state.get(qid)
            if c and (c["limit"] is None or c["count"] < c["limit"]):
                c["count"] += 1
    return terminal, seconds


# ------------------------------------------------------------------ persona-driven pre-test
def _persona_answer(q, persona, llm, rng):
    """One persona's answer. An explicit persona `answers[qid]` wins (exact control); else the LLM
    role-plays single-select / number / text; the engine still validates either way. Multi-select /
    grid / ranking without an explicit answer fall back to a random valid answer."""
    explicit = persona.get("answers") if isinstance(persona, dict) else None
    if explicit and q["id"] in explicit:
        return explicit[q["id"]]                 # exact, author-supplied — engine validates it
    t = q["type"]
    if t in ("radio", "dropdown", "rating", "nps", "number", "textarea", "text"):
        try:
            a = llm.respond(persona, q)
        except Exception:
            a = None
        if a is not None and a != "":
            if t == "number":
                try:
                    return int(float(a))
                except (TypeError, ValueError):
                    return answer_question(q, rng)
            opts = [o["value"] for o in q.get("options", [])] or list(q.get("scale_points", []))
            if not opts or a in opts:
                return a
    return answer_question(q, rng)


def run_one_persona(rt, quota_state, uid, persona, llm, rng, lock=None):
    url_vars = {rt.respondent_var: str(uid), rt.response_var: str(7000 + uid),
                "group": "pretest", "lang": "en"}
    session = rt.new_session(f"persona:{uid}", url_vars)
    step = rt.start(session, quota_state)
    answers, guard = {}, 0
    while "page" in step and guard < rt.max_steps:
        guard += 1
        page = step["page"]
        page_answers = {q["id"]: _persona_answer(q, persona, llm, rng)
                        for q in page["questions"] if q["type"] not in ("instructions", "video", "image")}
        answers.update(page_answers)
        step = rt.submit(session, page["page_id"], page_answers, quota_state)
    terminal = step["terminal"]
    if terminal["status"] == STATUS_COMPLETED:
        with (lock or nullcontext()):            # quota_state is shared when personas run in parallel
            for qid in rt.quota_buckets(session):
                c = quota_state.get(qid)
                if c and (c["limit"] is None or c["count"] < c["limit"]):
                    c["count"] += 1
    return terminal, answers


def persona_pretest(spec, n=None, personas=None, audience=None, llm=None, source=None, seed=7, workers=8):
    """Run a set of persona respondents as a pre-test: who qualifies, who screens out (and why), and a
    realistic incidence — richer than the uniform-random coverage gate because answers come from
    role-played people, not coin flips. Personas come from a `persona_source` (default `synthetic`:
    LLM, or the offline stub); pass an `audience` (weighted-segment population) to test against a
    *declared* population, or explicit `personas`. Engine stays authoritative; answers are validated.

    With a real LLM each persona makes ~one request PER question, so cost/time scale with the count —
    keep `n` small (the explicit `n` wins over an audience file's `n`). Persona runs are parallelized.
    NOTE: synthetic respondents are for TESTING, not a substitute for fielding to real people."""
    from survey_agent import ai
    from survey_agent.personas import get_persona_source, load_audience
    llm = llm or ai.get_llm()
    rt = SurveyRuntime(spec)

    aud = load_audience(audience) if audience else None
    # explicit n wins; else the audience file's n; else a small default (a real-LLM pre-test is N×questions calls)
    n = n if n is not None else ((aud.get("n") if isinstance(aud, dict) else None) or 6)
    src = source or get_persona_source()
    if personas is not None:                       # explicit personas (strings or {label,traits})
        personas = [p if isinstance(p, dict) else {"label": p, "traits": {}, "segment": None} for p in personas]
        source_name = "explicit"
    else:
        personas = src.generate(spec, aud, n=n, seed=seed)
        source_name = getattr(src, "name", "?")

    quota_state = rt.initial_quota_state()
    lock = threading.Lock()

    def _run(item):
        i, p = item
        rng_i = random.Random(seed * 1009 + i)     # per-persona rng: thread-safe + deterministic
        return (p,) + run_one_persona(rt, quota_state, i, p, llm, rng_i, lock=lock)

    # personas are independent + I/O-bound (one LLM round-trip per question) -> run them concurrently
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(personas) or 1))) as ex:
        runs = list(ex.map(_run, enumerate(personas, 1)))   # ex.map preserves input order

    results, statuses, reasons = [], Counter(), Counter()
    for p, terminal, answers in runs:
        statuses[terminal["status"]] += 1
        if terminal.get("reason_code"):
            reasons[terminal["reason_code"]] += 1
        results.append({"persona": p.get("label", str(p)), "traits": p.get("traits", {}),
                        "segment": p.get("segment"), "status": terminal["status"],
                        "reason_code": terminal.get("reason_code"), "answers": answers})
    total = len(personas) or 1
    return {
        "id": rt.d.get("id"), "llm": getattr(llm, "name", "?"), "source": source_name,
        "audience": bool(aud), "n": len(personas),
        "results": results, "statuses": dict(statuses), "reasons": dict(reasons),
        "incidence": statuses.get(STATUS_COMPLETED, 0) / total,
    }


def format_pretest(res):
    """Human-readable persona pre-test summary: incidence, outcome/screen-out breakdown, and per-
    question answer distributions across the respondents. Mirrors format_coverage's role for the
    persona pre-test (the connector / admin API returns this alongside the structured result)."""
    n = res.get("n", 0) or 0
    completed = res.get("statuses", {}).get(STATUS_COMPLETED, 0)
    lines = [
        f"Pre-test: {res.get('id')}  ·  {n} persona respondent(s)  ·  "
        f"llm={res.get('llm')}  source={res.get('source')}"
        f"{'  (vs declared audience)' if res.get('audience') else ''}",
        f"Incidence (completed / total): {res.get('incidence', 0.0) * 100:.0f}%  ({completed}/{n})",
    ]
    if res.get("statuses"):
        lines.append("\nOutcomes:")
        for status, c in sorted(res["statuses"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {status:<14} {c}")
    if res.get("reasons"):
        lines.append("\nScreen-out reasons:")
        for reason, c in sorted(res["reasons"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<24} {c}")
    dist = {}
    for r in res.get("results", []):
        for qid, ans in (r.get("answers") or {}).items():
            for v in (ans if isinstance(ans, list) else [ans]):
                dist.setdefault(qid, Counter())[str(v)[:60]] += 1
    if dist:
        lines.append("\nAnswer distributions:")
        for qid, counter in dist.items():
            lines.append(f"  {qid}:")
            for val, c in counter.most_common():
                lines.append(f"     {c:>3}  {val}")
    return "\n".join(lines)


def coverage(spec, n=200, seed=7, quota_override=None):
    """Run N synthetic respondents and return a STRUCTURED coverage result (no printing, no exit).

    This is the feedback signal the agentic `author` loop self-corrects against. Beyond the raw
    status/reason/event counts it reports `problems`: a list of plain-language coverage gaps — the
    default `complete` being unreachable, or a DEFINED screen-out that never fired (a dead branch the
    model should fix). `problems` is empty when every qualified path can finish and every declared
    screen-out is reachable. A bare LLM prompt has no way to know any of this; the engine does."""
    rt = SurveyRuntime(spec)
    quota_state = rt.initial_quota_state()
    if quota_override:
        for c in quota_state.values():
            if c["limit"] is not None:
                c["limit"] = quota_override
    rng = random.Random(seed)
    targets = _numeric_targets(spec)             # straddle numeric branches so coverage isn't flaky

    statuses, reasons, events = Counter(), Counter(), Counter()
    completed_loi = []
    stuck_pages = set()
    blanks, seen = Counter(), Counter()
    for uid in range(1, n + 1):
        t, secs = run_one(rt, quota_state, uid, rng, targets, blanks, seen)
        statuses[t["status"]] += 1
        if t["status"] == "stuck":
            stuck_pages.add(t.get("page_id"))
        if t.get("reason_code"):
            reasons[t["reason_code"]] += 1
        events[terminal_map.STATUS_TO_EVENT.get(t["status"], "?")] += 1
        if t["status"] == STATUS_COMPLETED:
            completed_loi.append(secs)

    dq_actions = [a for a in rt.actions if a.get("type") == "disqualify"]
    defined_reasons = sorted({a.get("reason_code") for a in dq_actions if a.get("reason_code")})
    problems = []
    if stuck_pages:
        pages = ", ".join(sorted(p for p in stuck_pages if p)) or "?"
        problems.append(f"respondents got stuck and could not advance past page(s) [{pages}] — a "
                        "question there is unanswerable (a required field whose validation no answer "
                        "can satisfy, or a required choice with no selectable option); fix the question "
                        "so a valid answer exists")
    if statuses.get(STATUS_COMPLETED, 0) == 0:
        if reasons:
            top = ", ".join(f"'{rc}' ({c}/{n})" for rc, c in reasons.most_common(3))
            problems.append(
                f"no respondent reached 'completed' — every path is caught by a screen-out first. "
                f"Dominant screen-out(s): {top}. Loosen or reorder the disqualify whose `when` is too "
                f"broad (or move it to a later page) so some respondents can finish.")
        else:
            problems.append(
                "no respondent reached 'completed' and none were screened out — the `complete` action "
                "is unreachable. Check the page flow, a required question with no valid answer, and any "
                "terminal that fires before the last page.")
    for rc in defined_reasons:
        if reasons.get(rc, 0) == 0:
            problems.append(f"screen-out '{rc}' is defined but never triggered across {n} respondents: "
                            f"its `when` condition is unreachable or wrong (a dead branch) — check the "
                            f"option value it compares against, the page order, and whether an earlier "
                            f"action always catches first")
    # Blank piping is a warning, like validate's: it's sometimes intended, and it shouldn't flip a
    # passing gate. Only answer-derived tokens are counted — the simulator can't know which URL
    # variables or context facts a real link/warehouse supplies (validate warns about those).
    blank_piping = [{"page": pid, "where": where, "token": tok, "count": c, "of": seen[pid]}
                    for (pid, where, tok), c in sorted(blanks.items(), key=lambda kv: -kv[1])]
    warnings = [f"piping: {b['token']} in {b['where']} rendered blank for {b['count']}/{b['of']} "
                f"respondents who saw page '{b['page']}' — the answer it reads can be missing "
                f"(optional, or on a page not everyone sees). Add a fallback like "
                f"{b['token'][:-2]}|…}}}}, or pipe from a required question on an always-shown page"
                for b in blank_piping]
    if dq_actions and not defined_reasons and statuses.get(STATUS_DISQUALIFIED, 0) == 0:
        problems.append("disqualify actions are defined but no respondent was ever screened out")

    return {
        "id": rt.d.get("id"), "version": rt.d.get("version", 1), "n": n,
        "statuses": dict(statuses), "reasons": dict(reasons), "events": dict(events),
        "quota": {qid: dict(c) for qid, c in quota_state.items()},
        "defined_reasons": defined_reasons,
        "fired_reasons": [r for r in defined_reasons if reasons.get(r, 0)],
        "loi_seconds": statistics.median(completed_loi) if completed_loi else None,
        "blank_piping": blank_piping,
        "warnings": warnings,
        "problems": problems,
    }


def format_report(cov):
    """The coverage report BODY as a string (status/reasons/passbacks/quota/LOI). Shared by the CLI
    and the MCP `simulate_survey` tool so both render identically without shelling out."""
    denom = cov["n"] or 1
    lines = [f"\nSimulated {cov['n']} respondents through '{cov['id']}' v{cov['version']}\n",
             "Terminal status:"]
    lines += [f"  {s:16} {c:5}  ({100*c/denom:.1f}%)"
              for s, c in sorted(cov["statuses"].items(), key=lambda kv: -kv[1])]
    lines.append("\nScreen-out reasons:")
    lines += [f"  {r:18} {c:5}" for r, c in sorted(cov["reasons"].items(), key=lambda kv: -kv[1])]
    lines.append("\nLifecycle events that would fire:")
    lines += [f"  {e:18} {c:5}" for e, c in sorted(cov["events"].items(), key=lambda kv: -kv[1])]
    lines.append("\nFinal quota counters:")
    lines += [f"  {qid:16} {c['count']}/{c['limit']}" for qid, c in cov["quota"].items()]
    if cov.get("loi_seconds"):
        lines.append(f"\nEstimated length of interview (median of completes): {cov['loi_seconds'] / 60:.1f} min "
                     f"({round(cov['loi_seconds'])}s)")
    return "\n".join(lines)


def gate_verdict(cov):
    """The coverage-gate verdict line(s) as a string — no printing, no exit (callers decide).
    Warnings (e.g. blank piping) are listed but never fail the gate."""
    warn = "".join("\n  ⚠ " + w for w in cov.get("warnings", []))
    if cov["problems"]:
        return "\n❌ coverage gate failed:\n" + "\n".join("  • " + p for p in cov["problems"]) + warn
    return ("\n✅ coverage gate passed: complete + every declared screen-out reachable; all callbacks mapped."
            + warn)


def format_coverage(cov):
    """Full human-readable coverage output (report body + gate verdict). The connector returns this."""
    return format_report(cov) + "\n" + gate_verdict(cov)


def _print_report(cov):
    print(format_report(cov))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("survey")
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--quota", type=int, help="override answer-quota limits (to exercise over-quota)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", help="save the structured coverage result to a JSON file for review")
    args = ap.parse_args(argv)

    with open(args.survey) as f:
        spec = json.load(f)
    cov = coverage(spec, n=args.n, seed=args.seed, quota_override=args.quota)
    _print_report(cov)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(cov, f, indent=2)
        print(f"\nwrote {args.out}")
    print(f"\nReview the run: survey-agent report {args.survey} --simulate {args.n} --open"
          f"  ·  survey-agent export {args.survey} --simulate {args.n} --format csv")

    # coverage gate (the `test-survey` skill blocks launch if this fails)
    print(gate_verdict(cov))
    if cov["problems"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
