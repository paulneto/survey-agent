"""Pluggable LLM client for the agentic features (authoring + LLM context enrichment +
conversational mode). Bring your own model: Claude (Anthropic) **or** GPT/Codex (OpenAI).

Offline-safe by design: with no key (or SURVEY_LLM=stub) a deterministic StubLLM is used, so demos
and tests run without one; with a key, a real provider makes the calls. All providers expose the
same tiny interface:

  author(brief, schema, prior, errors)   draft (or repair) a Survey spec from a plain-language brief
  analyze(task, inputs, output)          derive a value from respondent data (sentiment, summary, …)
  interpret(question, reply)             map a free-text reply to a valid answer (conversational mode)
  phrase(question, history)              ask a question conversationally

Provider selection (`SURVEY_LLM`): `auto` (default) picks Anthropic if ANTHROPIC_API_KEY is set,
else OpenAI if OPENAI_API_KEY is set, else the stub. Force one with `anthropic` / `openai` / `stub`.

This lives OUTSIDE engine/ on purpose — the engine stays pure and offline. NOTE: sending respondent
data to an LLM is opt-in and audited; that policy is enforced by the callers (enrich/conversation),
not here. This module is just the transport. (Authoring only ever sees survey *definitions*, never
respondent data — it's the always-on, low-risk boundary.)
"""
import hashlib
import json
import os
import re

ANALYSIS_MODELS = {"anthropic": "claude-opus-4-8", "openai": "gpt-5.1"}


def _llm_timeout():
    """Per-request timeout (seconds) for a real provider call. Env-tunable; bounded so one slow
    call can't hang an authoring/analysis run indefinitely."""
    try:
        return float(os.environ.get("SURVEY_LLM_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def _llm_retries():
    """How many times the SDK transparently retries a transient (network/5xx/429) error."""
    try:
        return int(os.environ.get("SURVEY_LLM_RETRIES", "2"))
    except ValueError:
        return 2


def get_llm():
    from survey_agent import env_select
    mode = (env_select("SURVEY_LLM", "auto") or "auto").lower()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    try:
        if mode in ("auto", "anthropic") and anthropic_key:
            return AnthropicLLM(anthropic_key)
        if mode in ("auto", "openai") and openai_key:
            return OpenAILLM(openai_key)
    except Exception:
        pass
    return StubLLM()


def has_real_llm():
    """True if a real provider key is configured (used to nudge the user when authoring offline)."""
    return get_llm().name != "stub"


# ----------------------------------------------------------------- authoring prompt (shared)
def _author_prompt(brief, schema, prior=None, errors=None):
    """Build the survey-authoring prompt. `schema` is the canonical JSON Schema (as a dict);
    on a repair pass, `prior` is the last (invalid) spec and `errors` the validator output."""
    parts = [
        "You are an expert survey methodologist authoring a survey for the survey-agent engine.",
        "Logic is DATA, never code: conditions are a small AST of {ref,operator,value} nodes joined "
        "by and/or — there is no scripting. Rules you MUST follow:",
        "- Output ONE JSON object of the form {\"survey\": {...}} and NOTHING else (no markdown fence, "
        "no prose).",
        "- A question that depends on a PRIOR answer goes on its own LATER page (the engine is "
        "server-driven, one page at a time — it cannot branch within a page).",
        "- Every screen-out is a `disqualify` action with a `reason_code`; include exactly one "
        "terminal `complete` action (no `when`) as the default end.",
        "- Declare every URL variable you reference in `settings.url_variables` (always include "
        "uid and rid).",
        "- Display-only stimulus types are never required and never validated: `instructions` "
        "(title + a Markdown `body` — headings/bullets/links/inline images, for a formatted how-to), "
        "`video` and `image` (a `media` object — `video`/`image` need `media.url`; "
        "`image` also takes optional `alt`/`caption`/`max_width`). Put the stimulus on one page and "
        "the question(s) about it on the next.",
        "- To ask the same thing for each option someone picked, add `repeat: {\"source\": <earlier "
        "choice question>}` to a page (or one question). Pipe the item with {{loop:label}}; each "
        "item's answer is stored as `qid[item]` (refer to it that way, or `qid[@]` for the current "
        "item inside a later repeat). Use token-safe option values (letters/digits/_/-).",
        "- Keep ids short and stable (kebab/snake), e.g. q_age, p_screen.",
        "The authored survey MUST validate against this JSON Schema:",
        json.dumps(schema),
    ]
    if prior is not None and errors:
        parts += [
            "Your previous draft FAILED validation and/or a coverage simulation. Here it is:",
            json.dumps(prior),
            "Fix exactly these problems and return the corrected full survey. A `[coverage]` problem "
            "means the survey is schema-valid but a path doesn't work when the engine actually runs "
            "it on synthetic respondents — so re-examine the logic AST: each `when` condition, the "
            "exact option values it compares against, the page ordering, and which action catches "
            "first (precedence is disqualify → overquota → skip → complete). An `[incidence]` problem "
            "means a realistic audience qualifies at the wrong rate — adjust how strict the "
            "screen-outs are (loosen/remove a disqualify to raise incidence; add/tighten one to lower it). "
            "An `[loi]` problem means it's too long — remove or shorten questions, or merge pages.",
            "\n".join(f"- {e}" for e in errors),
        ]
    parts += ["Brief from the user:", brief or "(no brief given — produce a short, sensible screener)"]
    return "\n\n".join(parts)


def _extract_json(text):
    """Pull the first JSON object out of an LLM reply, tolerating stray prose or code fences."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON object found in model reply")


def _extract_json_array(text):
    """Pull a JSON array (e.g. a persona list) out of a model reply, tolerating prose/fences."""
    text = (text or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    for candidate in (text, m.group(0) if m else None):
        if not candidate:
            continue
        try:
            v = json.loads(candidate)
            if isinstance(v, list):
                return v
        except Exception:
            pass
    # last resort: split bullet / numbered lines
    return [re.sub(r"^[-*\d.)\s]+", "", ln).strip() for ln in text.splitlines() if ln.strip()]


# ----------------------------------------------------------------- persona simulation (shared)
# Persona-driven synthetic respondents: richer than uniform-random answers, so a pre-launch test
# surfaces confusing wording and a realistic incidence — not just whether every branch is reachable.
# The engine stays authoritative (same contract as conversational mode): the LLM only picks an
# answer for a persona; runtime validates it and decides flow.
# a deliberately mixed offline population: qualifiers AND likely screen-outs, so a no-key pre-test
# still shows a realistic funnel rather than 100% (the real LLM gets the same instruction).
_FALLBACK_PERSONAS = [
    "a budget-conscious student who streams on a shared account",
    "a busy parent with very little free time",
    "a heavy subscriber who pays for several streaming services",
    "a tech enthusiast who tries every new platform",
    "someone who has never paid for a streaming service and only watches free TV",
    "a retiree who cancelled all streaming and doesn't use apps",
    "a privacy-minded person who would decline an optional study",
    "a cord-cutter who replaced cable entirely with streaming",
]


def _hash_pick(seed, n):
    """Deterministic index in [0,n) from a string — lets the offline stub give each persona a
    stable, varied answer (different personas differ; the same persona is consistent)."""
    return 0 if n <= 1 else int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % n


def _survey_blurb(spec):
    s = spec.get("survey", spec) if isinstance(spec, dict) else {}
    bits, qtitles = [s.get("title", "")], []
    for p in s.get("pages", []):
        for q in p.get("questions", []):
            if q.get("type") == "instructions" and q.get("body"):
                bits.append(q["body"])
            elif q.get("title") and q.get("type") not in ("instructions", "video", "image"):
                qtitles.append(q["title"])
    return " / ".join(b for b in (bits + qtitles[:6]) if b)[:600]


def _personas_prompt(spec, n):
    return (f"Generate exactly {n} short, diverse, realistic personas spanning the BROAD population a "
            f"survey like this is fielded to — NOT just the ideal qualifier. Deliberately include some "
            f"who would likely screen OUT (e.g. don't use the product/category, fall outside the target "
            f"age, or would decline to take part), so a pre-test reveals realistic screen-outs and "
            f"incidence — not 100%. One sentence each, varied on the traits the survey screens for. "
            f"Return ONLY a JSON array of strings.\n\nSurvey: {_survey_blurb(spec)}")


def persona_text(persona):
    """A profile string for prompts. Accepts a free-text persona OR a {label, traits} dict."""
    if isinstance(persona, dict):
        label = persona.get("label") or persona.get("description") or ""
        traits = "; ".join(f"{k}: {v}" for k, v in (persona.get("traits") or {}).items())
        return ", ".join(p for p in (label, traits) if p) or "a typical respondent"
    return str(persona)


def persona_traits(persona):
    return (persona.get("traits") or {}) if isinstance(persona, dict) else {}


def _respond_prompt(persona, question):
    opts = question.get("options")
    base = (f"You are an ATTENTIVE, good-faith survey respondent with this profile: "
            f"{persona_text(persona)}.\nAnswer the way this person honestly would, consistent with the "
            f"profile. You are paying attention: if the question contains an explicit instruction (e.g. "
            f"an attention/quality check like \"select 'Strongly agree'\"), follow it EXACTLY rather "
            f"than answering by sentiment.\n\nQuestion: {question.get('title')}")
    if opts:
        listing = "; ".join(f"{o['value']} ({o.get('label', '')})" for o in opts)
        return base + f"\nReply with ONLY one option value from: {listing}"
    if question.get("type") == "number":
        v = question.get("validation", {})
        span = f" between {v.get('min')} and {v.get('max')}" if v.get("min") is not None else ""
        return base + f"\nReply with ONLY a number{span}."
    return base + "\nReply with ONLY one or two short sentences."


def _validate_option(out, opts):
    if out in {str(o["value"]) for o in opts}:
        return out
    for o in opts:                      # case-insensitive value match
        if str(o["value"]).lower() == (out or "").lower():
            return o["value"]
    return None


# multi-select question types: their answer is a LIST of option values, not a single one
_MULTI_TYPES = ("checkbox", "dropdown_multi")
_AFFIRMATIVE = {"yes", "y", "yeah", "yep", "sure", "ok", "okay"}


def _match_options(text, opts, multi):
    """Map free text — a respondent's reply, or an LLM's interpretation of it — to option value(s).
    `multi` → a LIST of values in option order (or None if nothing matched); single → one value or
    None. Tolerant of comma / slash / 'and' separated lists and of labels embedded in a sentence."""
    r = str(text or "").strip().lower()
    if not r or r == "none":
        return None
    found = []
    def add(v):
        if v not in found:
            found.append(v)
    tokens = [t.strip() for t in re.split(r"[,/;\n]|\band\b|&", r) if t.strip()]
    for tok in tokens:                                   # exact match on a value or label
        for o in opts:
            if tok == str(o["value"]).lower() or tok == str(o.get("label", "")).lower():
                add(o["value"])
    if multi or not found:                               # label appears in the reply (longest first)
        for o in sorted(opts, key=lambda o: -len(str(o.get("label", "")))):
            lab = str(o.get("label", "")).lower()
            if lab and lab in r:
                add(o["value"])
    if not found:                                        # value appears (skip 1-2 char false positives)
        for o in opts:
            v = str(o["value"]).lower()
            if len(v) > 2 and v in r:
                add(o["value"])
    if not found and not multi and r in _AFFIRMATIVE:    # "yes" → a yes-ish option
        for o in opts:
            if str(o["value"]).lower() in ("yes", "y", "true"):
                add(o["value"])
    if not found:
        return None
    if multi:
        order = {o["value"]: i for i, o in enumerate(opts)}
        return sorted(found, key=lambda v: order.get(v, len(opts)))
    return found[0]


def _interpret_prompt(question, reply, multi):
    allowed = ", ".join(str(o["value"]) for o in question.get("options", []))
    if multi:
        return (f"Survey question (the respondent may choose MORE THAN ONE): {question.get('title')}\n"
                f"Valid option values: {allowed}\n"
                f"Respondent said: \"{reply}\"\n"
                f"Reply with ALL matching option values as a comma-separated list (e.g. netflix, max), "
                f"using ONLY values from the list above. Reply NONE if none apply.")
    return (f"Survey question: {question.get('title')}\nValid option values: {allowed}\n"
            f"Respondent said: \"{reply}\"\nReply with ONLY the single best-matching option value, or NONE.")


def _probe_prompt(question, answer):
    """One neutral, non-leading follow-up to draw out a thin open-text answer (adaptive probing)."""
    return (f"In a survey, the respondent was asked: \"{question.get('title')}\"\n"
            f"They answered briefly: \"{answer}\"\n"
            f"Ask ONE short, neutral follow-up question to draw out a little more detail — just the "
            f"question, no preamble, and don't lead them. If the answer already has enough detail, "
            f"reply with exactly NONE.")


# ----------------------------------------------------------------- auto-insight (analysis at fielding end)
def _insights_prompt(summary, open_ends):
    oe = "\n".join(f"- [{q}] " + "  |  ".join(str(a) for a in vals[:8]) for q, vals in (open_ends or {}).items())
    return ("You are a senior survey research analyst. Given the aggregated results and a sample of "
            "open-text answers from a completed survey, write a tight, decision-useful read-out. "
            "Return ONLY a JSON object with keys: headline (one sentence), findings (3-6 strings, "
            "specific and quantitative — cite the actual numbers, never invent them), themes (array "
            "of {theme, note} distilled from the open-ends), watchouts (1-3 data-quality / bias / "
            "small-sample caveats), next_steps (2-3 concrete follow-up questions to ask next wave).\n\n"
            f"RESULTS (JSON):\n{json.dumps(summary)[:6000]}\n\nOPEN-TEXT SAMPLE:\n{oe[:3000]}")


def _stub_insights(summary, open_ends=None):
    """Deterministic offline insight: mechanical (counts, not interpretation), but genuinely useful
    so `--insights` always produces something. A real key turns this into narrative analysis."""
    n = summary.get("n", 0)
    completed = (summary.get("funnel", {}).get("counts") or [0])[0]
    inc = summary.get("incidence", 0)
    findings = [f"{completed} of {n} responses completed (incidence {inc}%)."]
    reasons = summary.get("reasons", {})
    if reasons.get("labels"):
        top = max(zip(reasons["labels"], reasons["counts"]), key=lambda x: x[1])
        findings.append(f"Most common screen-out: '{top[0]}' ({top[1]} respondents).")
    if (summary.get("loi") or {}).get("median") is not None:
        findings.append(f"Median length of interview: {summary['loi']['median']}s.")
    themes = []
    for q in summary.get("questions", []):
        labels = q.get("labels") or []
        if q.get("kind") != "count" and labels and q.get("type") not in ("textarea", "text"):
            top = max(zip(labels, q["counts"]), key=lambda x: x[1])
            findings.append(f"“{q['title'][:60]}” — top answer “{top[0]}” ({top[1]}).")
        elif q.get("type") in ("textarea", "text") and labels:
            themes.append({"theme": ", ".join(labels[:5]), "note": f"top terms in “{q['title'][:50]}”"})
    return {
        "headline": f"{completed}/{n} completed ({inc}% incidence); {len(summary.get('questions', []))} questions analysed.",
        "findings": findings[:6],
        "themes": themes,
        "watchouts": ["Generated offline (no LLM key): mechanical counts, not interpretation — set "
                      "ANTHROPIC_API_KEY or OPENAI_API_KEY for narrative analysis of the open-ends."],
        "next_steps": (["Add a follow-up on the top screen-out reason to recover incidence."]
                       if reasons.get("labels") else
                       ["Probe the leading open-end theme with a targeted question next wave."]),
        "engine": "stub",
    }


# ----------------------------------------------------------------- methodology critic (design review)
_LEADING = re.compile(r"\b(don'?t you|do you agree|wouldn'?t you|isn'?t it|how (?:great|good|much better)|"
                      r"obviously|surely|as you know|everyone (?:knows|agrees))\b", re.I)


def _critique_prompt(survey):
    return ("You are an expert survey methodologist. Review this survey for problems that bias or "
            "degrade the data — leading/loaded wording, double-barreled questions (two asks in one), "
            "unbalanced or missing-midpoint scales, missing neutral / 'prefer not to say' / 'none' "
            "options, overlapping or gapped numeric ranges, jargon, acquiescence bias, and sensitive "
            "questions with no opt-out. For each, return {id (question id, or 'survey'), severity "
            "(high|medium|low), type, issue, suggestion}. Return ONLY JSON {issues:[...], summary: one "
            "sentence}. Empty issues array if it's clean. Don't invent questions.\n\n"
            f"SURVEY (JSON):\n{json.dumps(survey)[:7000]}")


def _stub_critique(survey):
    """Offline heuristic review — high-precision rules only (advisory). A real key gives a fuller,
    judgment-based methodology read. Sees the definition only — never respondent data."""
    s = survey.get("survey", survey) if isinstance(survey, dict) else {}
    issues = []
    opt_out = ("none", "prefer not", "not sure", "don't know", "dont know", "n/a", "decline", "other")
    for p in s.get("pages", []):
        for q in p.get("questions", []):
            t, title = q.get("type"), (q.get("title") or "")
            if t in ("video", "image", "instructions") or not title:
                continue
            if _LEADING.search(title):
                issues.append({"id": q["id"], "severity": "high", "type": "leading",
                               "issue": "Wording may push the respondent toward an answer.",
                               "suggestion": "Rephrase neutrally (e.g. 'How satisfied are you with X?')."})
            if "?" in title and re.search(r"\w+\s+and\s+\w+", title.lower()) \
                    and t in ("radio", "rating", "nps", "dropdown", "checkbox"):
                issues.append({"id": q["id"], "severity": "medium", "type": "double_barreled",
                               "issue": "Appears double-barreled (asks about two things at once).",
                               "suggestion": "Split it so each question measures one thing."})
            if t in ("radio", "dropdown") and len(q.get("options", [])) >= 3:
                labels = " ".join(str(o.get("label", o.get("value", ""))).lower() for o in q["options"])
                if not any(k in labels for k in opt_out):
                    issues.append({"id": q["id"], "severity": "low", "type": "no_opt_out",
                                   "issue": "No neutral / 'prefer not to say' / 'none' option.",
                                   "suggestion": "Add an opt-out so respondents aren't forced to pick."})
            ranges = []
            for o in q.get("options", []):
                m = re.match(r"\s*(\d+)\s*(?:[-–—]|to)\s*(\d+)", str(o.get("label", o.get("value", ""))))
                if m:
                    ranges.append((int(m.group(1)), int(m.group(2))))
            ranges.sort()
            if any(ranges[i][1] >= ranges[i + 1][0] for i in range(len(ranges) - 1)):
                issues.append({"id": q["id"], "severity": "medium", "type": "overlapping_ranges",
                               "issue": "Numeric option ranges overlap (an answer fits two buckets).",
                               "suggestion": "Use non-overlapping bands, e.g. 0–9, 10–19, 20–29."})
            if len(title) > 160:
                issues.append({"id": q["id"], "severity": "low", "type": "wording",
                               "issue": "Long/complex wording (>160 chars).",
                               "suggestion": "Tighten to one clear sentence."})
    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: order.get(i["severity"], 3))
    counts = {sev: sum(1 for i in issues if i["severity"] == sev) for sev in ("high", "medium", "low")}
    summary = ("; ".join(f"{counts[s]} {s}" for s in ("high", "medium", "low") if counts[s]) + " issue(s)"
               if issues else "No obvious methodology issues found (heuristic check).")
    return {"issues": issues, "summary": summary, "engine": "stub"}


def _stub_authored_survey(brief):
    """A deterministic, schema-valid minimal screener the offline stub returns so `author` always
    produces something runnable (consent → one open-text question → complete; disqualify on no
    consent). A real key produces a far richer survey from the brief."""
    words = re.findall(r"[a-z0-9]+", (brief or "draft survey").lower())
    slug = "-".join(words[:4]) or "draft-survey"
    topic = " ".join((brief or "your experience").split()[:12]) or "your experience"
    return {"survey": {
        "id": slug,
        "version": 1,
        "title": topic[:60].strip().capitalize() or "Draft survey",
        "settings": {"url_variables": ["uid", "rid"], "theme": "paper"},
        "pages": [
            {"id": "p_consent", "questions": [
                {"id": "q_consent", "type": "radio", "required": True,
                 "title": f"This short study is about: {topic}. Do you consent to take part?",
                 "options": [{"value": "yes", "label": "Yes, I consent"},
                             {"value": "no", "label": "No"}]}]},
            {"id": "p_main", "questions": [
                {"id": "q_open", "type": "textarea", "required": True,
                 "title": f"In your own words, tell us about: {topic}"}]},
        ],
        "actions": [
            {"type": "disqualify", "reason_code": "no_consent",
             "dq_message": "No problem — thanks for stopping by.",
             "when": {"ref": {"kind": "question", "id": "q_consent"},
                      "operator": "equals", "value": "no"}},
            {"type": "complete", "redirect": {"kind": "default"}},
        ],
    }}


# ----------------------------------------------------------------- stub (offline, deterministic)
_POS = ("love", "great", "good", "helpful", "like", "best", "amazing", "works", "happy", "easy")
_NEG = ("bad", "hate", "worse", "side effect", "problem", "expensive", "stop", "quit", "nausea",
        "confus", "hard", "disappoint")


def _sentiment(text):
    t = (text or "").lower()
    p = sum(w in t for w in _POS)
    n = sum(w in t for w in _NEG)
    return "positive" if p > n else "negative" if n > p else "neutral"


def _theme(text):
    for t in ("side effects", "cost", "results", "dosage", "availability", "price", "support"):
        if t in (text or "").lower():
            return t
    words = (text or "").split()
    return " ".join(words[:3]) if words else "general"


class StubLLM:
    name = "stub"

    def author(self, brief, schema, prior=None, errors=None):
        # offline: return a deterministic, schema-valid minimal screener (no model call)
        return _stub_authored_survey(brief)

    def analyze(self, task, inputs, output=None):
        text = " ".join(str(v) for v in inputs.values() if v)
        otype = (output or {}).get("type", "text")
        if otype == "json":
            return {"sentiment": _sentiment(text), "theme": _theme(text)}
        if otype == "enum":
            return _sentiment(text)
        parts = [f"{k}={v}" for k, v in inputs.items() if v]
        return "Respondent: " + "; ".join(parts[:5]) if parts else "Respondent (no detail provided)."

    def interpret(self, question, reply):
        return _interpret_stub(question, reply)

    def phrase(self, question, history=None):
        return question.get("title", "")

    def personas(self, spec, n):
        return [_FALLBACK_PERSONAS[i % len(_FALLBACK_PERSONAS)] for i in range(max(1, n))]

    def respond(self, persona, question):
        # deterministic, persona-flavored answer (offline): stable per (persona, question), and
        # *coherent* with declared traits where the survey asks about them — so a trait like
        # usage="never" reliably hits the "never" option (and its screen-out) without a key.
        opts = question.get("options")
        text = persona_text(persona)
        blob = (text + " " + " ".join(str(v) for v in persona_traits(persona).values())).lower()
        key = f"{text}|{question.get('id')}"
        if opts:
            ranked = sorted(opts, key=lambda o: -len(str(o.get("label", ""))))  # most specific label first
            for o in ranked:                     # 1. coherence: a declared-trait match wins
                lab, val = str(o.get("label", "")).lower(), str(o["value"]).lower()
                if (len(lab) > 2 and lab in blob) or (len(val) > 2 and val in blob):
                    return o["value"]
            title = (question.get("title") or "").lower()   # 2. comply with an instruction in the prompt
            for o in ranked:                                 # (e.g. an attention check names its answer)
                lab = str(o.get("label", "")).lower()
                if len(lab) > 2 and lab in title:
                    return o["value"]
            # 3. else bias toward earlier options (qualifying/affirmative listed first) — min-of-two skew
            idx = min(_hash_pick(key + "#1", len(opts)), _hash_pick(key + "#2", len(opts)))
            return opts[idx]["value"]
        if question.get("type") == "number":
            v = question.get("validation", {})
            lo, hi = int(v.get("min", 18)), int(v.get("max", 80))
            m = re.search(r"\d{1,3}", blob)      # honor a numeric trait (e.g. age) if present
            if m and lo <= int(m.group(0)) <= hi:
                return m.group(0)
            return str(lo + _hash_pick(key, max(1, hi - lo + 1)))
        return f"Speaking as {text}, that's my honest take."

    def probe(self, question, answer):
        # offline: a neutral generic follow-up (a real model tailors it to the answer)
        return "Thanks — could you say a bit more about why?"

    def insights(self, summary, open_ends=None):
        return _stub_insights(summary, open_ends)

    def critique(self, survey):
        return _stub_critique(survey)


def _interpret_stub(question, reply):
    opts = question.get("options")
    if opts:
        return _match_options(reply, opts, question.get("type") in _MULTI_TYPES)
    if question.get("type") == "number":
        m = re.search(r"-?\d+(\.\d+)?", str(reply or ""))
        return m.group(0) if m else None
    return reply


# ----------------------------------------------------------------- real providers (shared logic)
class _BaseLLM:
    """Shared implementation for the real providers. The whole agentic surface (author / analyze /
    interpret / phrase / personas / respond / probe / insights / critique) is provider-agnostic —
    it's all prompt-building (above) + one chat round-trip — so a subclass only supplies `name`,
    `__init__` (the SDK client), and `_msg(prompt, max_tokens)`. This keeps Anthropic and OpenAI from
    drifting apart (they used to be near-identical copies)."""
    name = "base"

    def _msg(self, prompt, max_tokens=400):           # pragma: no cover - subclass responsibility
        raise NotImplementedError

    def author(self, brief, schema, prior=None, errors=None):
        prompt = _author_prompt(brief, schema, prior, errors)
        try:
            return _extract_json(self._msg(prompt, max_tokens=8000))
        except ValueError:
            # The reply was prose or truncated JSON (no parseable object). One repair retry with a
            # terser instruction — so a single garbled response doesn't abort the whole run. (Transient
            # network/5xx/429 errors are already retried inside the SDK via max_retries.)
            retry = prompt + "\n\nReturn ONLY the JSON object — no prose, no markdown fence."
            return _extract_json(self._msg(retry, max_tokens=8000))

    def analyze(self, task, inputs, output=None):
        otype = (output or {}).get("type", "text")
        ctx = "\n".join(f"- {k}: {v}" for k, v in inputs.items())
        if otype in ("json", "enum"):
            shape = "a JSON object" if otype == "json" else "a single value"
            txt = self._msg(f"{task}\n\nRespondent data:\n{ctx}\n\nReturn ONLY {shape}, no prose.")
            try:
                return _extract_json(txt) if otype == "json" else txt.strip().strip('"')
            except Exception:
                return txt
        return self._msg(f"{task}\n\nRespondent data:\n{ctx}")

    def interpret(self, question, reply):
        opts = question.get("options")
        if opts:
            multi = question.get("type") in _MULTI_TYPES
            v = self._msg(_interpret_prompt(question, reply, multi), 60 if multi else 16).strip().strip('"')
            return _match_options(v, opts, multi)
        if question.get("type") == "number":
            m = re.search(r"-?\d+(\.\d+)?", str(reply))
            return m.group(0) if m else None
        return reply

    def phrase(self, question, history=None):
        return self._msg("Ask this survey question conversationally in one short, friendly message "
                         f"(don't list options unless helpful):\n{question.get('title')}", 120)

    def personas(self, spec, n):
        arr = _extract_json_array(self._msg(_personas_prompt(spec, n), 700))
        arr = [str(x).strip() for x in arr if str(x).strip()][:n]
        return arr or _FALLBACK_PERSONAS[:n]

    def respond(self, persona, question):
        opts = question.get("options")
        out = self._msg(_respond_prompt(persona, question), 24 if opts else 64).strip().strip('"')
        if opts:
            return _validate_option(out, opts)
        if question.get("type") == "number":
            m = re.search(r"-?\d+(\.\d+)?", out)
            return m.group(0) if m else None
        return out

    def probe(self, question, answer):
        out = self._msg(_probe_prompt(question, answer), 50).strip().strip('"')
        return None if (not out or out.strip().upper() == "NONE") else out

    def insights(self, summary, open_ends=None):
        try:
            out = _extract_json(self._msg(_insights_prompt(summary, open_ends), 1200))
        except Exception:
            out = _stub_insights(summary, open_ends)   # fail-open to the mechanical read-out
        out.setdefault("engine", self.name)
        return out

    def critique(self, survey):
        try:
            out = _extract_json(self._msg(_critique_prompt(survey), 1500))
            out.setdefault("issues", [])
            out.setdefault("summary", "")
        except Exception:
            out = _stub_critique(survey)               # fail-open to the heuristic review
        out.setdefault("engine", self.name)
        return out


class AnthropicLLM(_BaseLLM):
    name = "anthropic"

    def __init__(self, key):
        import anthropic
        self._c = anthropic.Anthropic(api_key=key, timeout=_llm_timeout(), max_retries=_llm_retries())
        self.model = os.environ.get("SURVEY_LLM_MODEL", ANALYSIS_MODELS["anthropic"])

    def _msg(self, prompt, max_tokens=400):
        r = self._c.messages.create(model=self.model, max_tokens=max_tokens,
                                    messages=[{"role": "user", "content": prompt}])
        return "".join(getattr(b, "text", "") for b in r.content).strip()


class OpenAILLM(_BaseLLM):
    """OpenAI-compatible provider (GPT / Codex). Honours OPENAI_BASE_URL, so it also drives any
    OpenAI-compatible endpoint (Azure OpenAI, local servers, etc.)."""
    name = "openai"

    def __init__(self, key):
        import openai
        kwargs = {"api_key": key}
        base = os.environ.get("OPENAI_BASE_URL")
        if base:
            kwargs["base_url"] = base
        kwargs["timeout"] = _llm_timeout()
        kwargs["max_retries"] = _llm_retries()
        self._c = openai.OpenAI(**kwargs)
        self.model = os.environ.get("SURVEY_LLM_MODEL", ANALYSIS_MODELS["openai"])

    def _msg(self, prompt, max_tokens=400):
        r = self._c.chat.completions.create(
            model=self.model, max_completion_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        return (r.choices[0].message.content or "").strip()
