"""Publish-time validation: JSON-Schema structural check + semantic lint.

Run:  survey-agent validate surveys/streaming-usage.survey.json
The semantic lint is the load-bearing gate the authoring skills rely on: it catches dead
branches, dangling references, and missing terminals that a schema alone can't.
"""
import json
import os
import re
import sys

from .runtime import unwrap
from . import piping as _piping
from . import repeat as _repeat
from . import safety as _safety

# Merge tokens ({{q:…}} / {{url:…}} / {{var:…}} / {{enrich:…|behavior:…}}) inside any piped string —
# url/payload/redirect/message. Mirrors engine/piping.TOKEN so the lint catches typo'd references
# that would otherwise render as empty string at field time.
_TOKEN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema", "survey.schema.json")

# Choice questions need something to choose from; display-only / scalar types do not.
_CHOICE_TYPES = ("radio", "checkbox", "dropdown", "dropdown_multi", "ranking")

# operators that can be true when the answer is blank — a display condition using one doesn't prove
# the answer exists
_BLANK_TRUE = {"is_not_answered", "not_equals", "is_not_one_of"}

# Real logic/calc trees nest a handful deep. Anything far beyond this is malformed (e.g. an LLM
# emitting a runaway nested condition) and would hit Python's recursion limit while the engine
# evaluates it at field time. Catch it at the gate instead, where it's a clean error, not a crash.
_MAX_AST_DEPTH = 64


def load_schema():
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def schema_errors(spec):
    try:
        import jsonschema
    except ImportError:
        return ["[warn] jsonschema not installed — skipped structural schema validation"]
    validator = jsonschema.Draft202012Validator(load_schema())
    return [
        f"schema: /{'/'.join(map(str, e.path))}: {e.message}"
        for e in sorted(validator.iter_errors(spec), key=lambda e: list(e.path))
    ]


def _too_deep(kind, where, depth, errors):
    if depth <= _MAX_AST_DEPTH:
        return False
    msg = (f"{where}: {kind} nests deeper than {_MAX_AST_DEPTH} levels — likely malformed "
           f"(would fail at field time)")
    if msg not in errors:
        errors.append(msg)
    return True


def _walk_conditions(node, where, check_ref, errors, depth=0):
    if not node:
        return
    if _too_deep("logic", where, depth, errors):
        return
    if "conditions" in node:
        for c in node["conditions"]:
            _walk_conditions(c, where, check_ref, errors, depth + 1)
    elif "ref" in node:
        check_ref(node["ref"], where, errors)
        if node.get("operator") == "regex_match":
            problem = _safety.regex_problem(node.get("value"))
            if problem:
                errors.append(f"{where}: regex_match {problem}")


def _walk_calc(node, where, check_ref, errors, depth=0):
    """Walk a calc AST (engine/calc.py) and check every `ref` it resolves. Nodes nest via
    `args` (add/sub/.../concat) and `of` (bucket/round); `ref`/`score`/`count_selected` carry a ref."""
    if not isinstance(node, dict):
        return
    if _too_deep("calc", where, depth, errors):
        return
    if "ref" in node:
        check_ref(node["ref"], where, errors)
    for child in node.get("args", []) or []:
        _walk_calc(child, where, check_ref, errors, depth + 1)
    if "of" in node:
        _walk_calc(node["of"], where, check_ref, errors, depth + 1)


def _check_tokens(text, where, qids, url_vars, var_names, enrich_ids, errors, answer_key=None,
                  in_loop=None, at_page=None, q_page=None, q_rows=None, may_blank=None, guards=(),
                  repeat_of=None):
    """Check that every {{…}} merge token in a piped string references something that exists, so a
    typo surfaces at validate time instead of silently rendering empty for a respondent.

    `answer_key(id, in_loop)` -> error message or None (repeat-aware question check); `in_loop` is the
    enclosing repeat spec (or None). `guards`: question ids the consuming question/page's display
    logic already tests, so a blank there can't be seen.
    For text a respondent sees (titles, bodies, labels) the caller also passes `at_page` — a token
    there can't read a question on that page or a later one — and `may_blank(qid)` -> reason or None,
    which turns a token with no `|fallback` that could render blank into a `[warn]`."""
    if not isinstance(text, str) or "{{" not in text:
        return
    raws = _TOKEN.findall(text)
    if "{{" in _TOKEN.sub("", text) or any("{" in r or "}" in r for r in raws):
        errors.append(f"{where}: malformed merge token — braces can't appear inside a token or its "
                      f"fallback, and tokens can't be nested (it would show raw braces to a respondent)")
        return
    for raw in raws:
        tok = "{{" + raw + "}}"
        kind, rest, fallback = _piping.split_token(raw)
        if kind not in _piping.KINDS:
            errors.append(f"{where}: unknown merge token {tok} — kinds are "
                          f"{', '.join(k + ':' for k in _piping.KINDS)}")
            continue
        if kind == "q":
            qid, row, field = _piping.parse_q(rest)
            if field not in _piping.Q_FIELDS:
                errors.append(f"{where}: merge token {tok} — unknown field '{field}'; use .answer "
                              f"(the label) or .value (the stored value)")
                continue
            msg = answer_key(qid, in_loop) if answer_key else (
                None if qid in qids else f"references unknown question '{qid}'")
            if msg:
                errors.append(f"{where}: merge token {msg}")
                continue
            base, item = _repeat.split_instance(qid)
            if row is not None and q_rows is not None and row not in q_rows.get(base, ()):
                errors.append(f"{where}: merge token {tok} — '{base}' has no grid row '{row}'")
            same_loop = (in_loop is not None and repeat_of is not None and repeat_of.get(base) is in_loop
                         and item not in (None, _repeat.CURRENT))
            if at_page is not None and q_page is not None and q_page.get(base, -1) == at_page and same_loop:
                # another item's answer from an earlier pass of this same repeated page
                if fallback is None:
                    errors.append(f"[warn] {where}: {tok} is blank until the '{item}' pass of this page "
                                  f"has been answered — add a fallback: {{{{q:{rest}|…}}}}")
                continue
            if at_page is not None and q_page is not None and q_page.get(base, -1) >= at_page:
                errors.append(f"{where}: merge token {tok} reads '{base}', which is on "
                              f"{'this' if q_page[base] == at_page else 'a later'} page — the page is "
                              f"built before that answer exists, so it would render blank. Pipe from "
                              f"an earlier page")
                continue
            if may_blank and fallback is None and base not in guards:
                why = may_blank(base)
                if not why and item not in (None, _repeat.CURRENT):
                    why = f"wasn't asked about '{item}' (per-item answers exist only for items repeated over)"
                if why:
                    errors.append(f"[warn] {where}: {tok} renders blank when '{base}' {why} — add a "
                                  f"fallback: {{{{{kind}:{rest}|…}}}}")
        elif kind == "loop":
            if not in_loop:
                errors.append(f"{where}: merge token {tok} is only meaningful inside a repeat")
            elif rest not in _repeat.LOOP_FIELDS:
                errors.append(f"{where}: merge token {tok} — loop fields are "
                              f"{', '.join(_repeat.LOOP_FIELDS)}")
        elif kind == "url":
            if rest not in url_vars:
                errors.append(f"{where}: merge token url '{rest}' not declared in settings.url_variables")
            elif may_blank and fallback is None:
                errors.append(f"[warn] {where}: {tok} renders blank when the link has no '{rest}' — add "
                              f"a fallback: {{{{url:{rest}|…}}}}")
        elif kind == "var":
            if rest not in var_names:
                errors.append(f"{where}: merge token var '{rest}' is not set by any set_variable/hidden")
        elif kind in _piping.ENRICH_KINDS:
            if rest not in enrich_ids:
                errors.append(f"{where}: merge token enrichment '{rest}' not declared in settings.enrichments")


def semantic_errors(spec):
    errors = []
    survey = unwrap(spec)
    url_vars = set(survey.get("settings", {}).get("url_variables", []))
    enrich_ids = set()
    for e in survey.get("settings", {}).get("enrichments", []):
        enrich_ids.add(e["id"] if isinstance(e, dict) else e)
    qids, page_ids = set(), set()
    for p in survey.get("pages", []):
        page_ids.add(p["id"])
        for q in p.get("questions", []):
            if q["id"] in qids:
                errors.append(f"duplicate question id '{q['id']}' — answers would overwrite each other")
            qids.add(q["id"])
    quota_ids = {qd.get("id") for qd in survey.get("quotas", [])}
    # Variables are defined by set_variable actions (`name`) and `hidden` calculations (`id`); a
    # {{var:…}} token must reference one of them.
    var_names = {a.get("name") for a in survey.get("actions", [])
                 if a.get("type") == "set_variable" and a.get("name")}
    var_names |= {h.get("id") for h in survey.get("hidden", []) if h.get("id")}

    # ---- repeating: which questions are repeated, over what, and where each lives
    page_pos, q_page = {}, {}
    for i, p in enumerate(survey.get("pages", [])):
        page_pos[p["id"]] = i
        for q in p.get("questions", []):
            q_page[q["id"]] = i
    qdefs = {q["id"]: q for p in survey.get("pages", []) for q in p.get("questions", [])}
    repeat_of = {}                                     # qid -> repeat spec its answers are keyed by
    for p in survey.get("pages", []):
        for q in p.get("questions", []):
            spec = p.get("repeat") or q.get("repeat")
            if isinstance(spec, dict):           # a non-object `repeat` is a schema error, reported there
                repeat_of[q["id"]] = spec

    page_list = survey.get("pages", [])
    q_rows = {q["id"]: {r.get("id") for r in q.get("rows", [])} for q in qdefs.values()}
    def _refs(node, out):
        if isinstance(node, dict):
            if "ref" in node:
                out.append(node["ref"])
            for c in node.get("conditions", []) or []:
                _refs(c, out)
        return out

    def _guards(node):
        """Questions this display logic proves are answered whenever it's true: tested (alone or under
        AND) by an operator that's false on a blank answer. An OR branch proves nothing."""
        if not isinstance(node, dict):
            return set()
        if "ref" in node:
            r = node["ref"]
            if r.get("kind") == "question" and node.get("operator") not in _BLANK_TRUE:
                return {_repeat.split_instance(r.get("id"))[0]}
            return set()
        conds = node.get("conditions", []) or []
        if node.get("op") == "and" or len(conds) == 1:
            return set().union(*(_guards(c) for c in conds)) if conds else set()
        return set()

    def _earliest_fire(when):
        """The last page a skip's `when` reads; it can first fire after that page (-1: from the start)."""
        pages = [q_page.get(_repeat.split_instance(r.get("id"))[0], -1) if r.get("kind") == "question"
                 else -1 for r in _refs(when, [])]
        return max(pages, default=-1)
    skip_spans = [(_earliest_fire(a.get("when")), page_pos[a["to_page"]]) for a in survey.get("actions", [])
                  if a.get("type") == "skip" and a.get("to_page") in page_pos]

    screened = set()          # questions some disqualify reads — the author has handled their edge cases

    def _collect(node):
        """Questions a screen-out tests on its own (the whole `when`, or a branch of an OR) — an AND
        with other terms doesn't count, since it may not fire when that question is blank/'None'."""
        if isinstance(node, dict):
            if "ref" in node and node["ref"].get("kind") == "question":
                screened.add(_repeat.split_instance(node["ref"].get("id"))[0])
            conds = node.get("conditions", []) or []
            if node.get("op") == "or" or len(conds) == 1:
                for c in conds:
                    _collect(c)
    for a in survey.get("actions", []):
        if a.get("type") == "disqualify":
            _collect(a.get("when"))

    def _has_exclusive(qid):
        return any(o.get("exclusive") for o in qdefs.get(qid, {}).get("options", []))

    def may_blank(qid):
        """Why an answer to `qid` might not exist when piped (None if it always will)."""
        q, pi = qdefs.get(qid, {}), q_page.get(qid)
        if pi is None:
            return None
        if not q.get("required"):
            return "is optional and left empty"
        if q.get("display_logic"):
            return "is hidden by its display logic"
        if page_list[pi].get("display_logic"):
            return "is on a page that isn't always shown"
        src = (q.get("options_source") or {}).get("pipe_from")
        if (src and src not in screened and q_page.get(src, pi) < pi
                and (may_blank(src) or _has_exclusive(src))):
            return "has no options to pick (its options are piped)"
        if any(fires < pi < t for fires, t in skip_spans):
            return "is on a page a skip can jump past"
        return None

    def repeat_items(spec):
        """The item values a repeat can produce (static — from the source's options or 1..max)."""
        if spec.get("by") == "value":
            return {str(i) for i in range(1, _repeat.cap(spec) + 1)}
        return {str(o.get("value")) for o in _repeat.source_options(spec.get("source"), qdefs)}

    def answer_key(key, in_loop):
        """None if `key` names a stored answer — a plain question, or an instance `q[item]` / `q[@]`
        of a repeated one — else the problem, phrased for an LLM author to act on."""
        base, item = _repeat.split_instance(key)
        if base not in qids:
            return f"references unknown question '{base}'"
        spec = repeat_of.get(base)
        if item is None:
            if spec:
                return (f"references repeated question '{base}' without an item — its answers are "
                        f"stored per item: use '{base}[<item>]', or '{base}[@]' inside a repeat")
            return None
        if not spec:
            return f"'{key}': '{base}' is not repeated, so it has no per-item answers — use '{base}'"
        if item == _repeat.CURRENT:
            if not in_loop:
                return f"'{key}': '[@]' (the current item) only works inside a repeat"
            def sig(r):
                return (r.get("source"), r.get("by", "answers"),
                        None if r.get("by") == "value" else r.get("mode", "selected"))
            if sig(spec) == sig(in_loop):
                return None
            return (f"'{key}': '{base}' repeats over {sig(spec)[0]!r} ({sig(spec)[2] or 'by value'}) but "
                    f"this repeat is over {sig(in_loop)[0]!r} ({sig(in_loop)[2] or 'by value'}) — '[@]' only "
                    f"matches items of a repeat over the same source and mode")
        if item not in repeat_items(spec):
            return f"'{key}': '{item}' is not an item '{base}' repeats over"
        return None

    def make_check_ref(in_loop, at_page=None):
        """`at_page`: the page whose rendering this logic decides — a ref to a question on that page
        or a later one can never hold an answer yet (pages are built server-side, before submit)."""
        def _check(ref, where, errs):
            kind = ref.get("kind")
            if kind == "question":
                msg = answer_key(ref.get("id"), in_loop)
                if msg:
                    errs.append(f"{where}: question ref {msg}")
                elif at_page is not None:
                    base = _repeat.split_instance(ref.get("id"))[0]
                    if q_page[base] >= at_page:
                        errs.append(f"{where}: depends on '{base}', which is on "
                                    f"{'this' if q_page[base] == at_page else 'a later'} page — the page is "
                                    f"built before that answer exists, so this never shows. Move the "
                                    f"dependent question to a later page")
            elif kind == "loop":
                if not in_loop:
                    errs.append(f"{where}: a 'loop' ref only works inside a repeat")
                elif ref.get("id") not in _repeat.LOOP_FIELDS:
                    errs.append(f"{where}: loop ref id must be one of {', '.join(_repeat.LOOP_FIELDS)}")
            else:
                check_ref(ref, where, errs)
        return _check

    def check_ref(ref, where, errs):
        kind = ref.get("kind")
        if kind in ("question", "loop"):
            return make_check_ref(False)(ref, where, errs)
        elif kind == "url_var" and ref.get("id") not in url_vars:
            errs.append(f"{where}: url_var '{ref.get('id')}' not declared in settings.url_variables")
        elif kind == "quota" and ref.get("id") not in quota_ids:
            errs.append(f"{where}: unknown quota ref '{ref.get('id')}'")
        elif kind == "enrichment" and ref.get("id") not in enrich_ids:
            errs.append(f"{where}: enrichment '{ref.get('id')}' not declared in settings.enrichments")

    def check_repeat(spec, where, at_page):
        src = spec.get("source")
        if src not in qids:
            errors.append(f"{where}: repeat.source '{src}' is unknown")
            return
        if q_page[src] >= at_page:
            errors.append(f"{where}: repeat.source '{src}' must be on an earlier page (the answer has to "
                          f"exist before the repeat starts)")
        if src in repeat_of:
            errors.append(f"{where}: repeat.source '{src}' is itself repeated — repeat over a plain question")
        sq = qdefs[src]
        if spec.get("by") == "value":
            numeric = sq.get("type") == "number" or (
                sq.get("type") in ("radio", "dropdown") and sq.get("options") and
                all(re.fullmatch(r"\d+", str(o.get("value"))) for o in sq["options"]))
            if not numeric:
                errors.append(f"{where}: repeat by 'value' needs a number source (or radio/dropdown with "
                              f"whole-number values); '{src}' is {sq.get('type')}")
            return
        if sq.get("type") not in _CHOICE_TYPES:
            errors.append(f"{where}: repeat.source '{src}' is {sq.get('type')} — repeat over a choice "
                          f"question ({', '.join(_CHOICE_TYPES)}), or set by: 'value' for a number")
            return
        values = repeat_items(spec)
        bad = sorted(v for v in values if not _repeat.SAFE_ITEM.match(v))
        if bad:
            errors.append(f"{where}: repeat.source '{src}' option values {bad} can't key per-item answers "
                          f"— use letters, digits, '_' or '-' as option values (labels can be anything)")
        for x in spec.get("exclude", []):
            if str(x) not in values:
                errors.append(f"{where}: repeat.exclude '{x}' is not an option of '{src}'")

    for pi, p in enumerate(survey.get("pages", [])):
        p_rep = p.get("repeat") if isinstance(p.get("repeat"), dict) else None
        page_loop = bool(p_rep)
        if page_loop:
            check_repeat(p_rep, f"page '{p['id']}'", pi)
        # page display_logic decides whether the page (the whole loop) runs — no item bound yet
        _walk_conditions(p.get("display_logic"), f"page '{p['id']}' display_logic",
                         make_check_ref(False, pi), errors)
        for q in p.get("questions", []):
            if isinstance(q.get("repeat"), dict):
                if page_loop:
                    errors.append(f"question '{q['id']}': can't repeat a question on a repeated page "
                                  f"'{p['id']}' — the page already repeats")
                else:
                    check_repeat(q["repeat"], f"question '{q['id']}'", pi)
            q_rep = q.get("repeat") if isinstance(q.get("repeat"), dict) else None
            in_loop = p_rep or q_rep
            qref = make_check_ref(in_loop, pi)
            _walk_conditions(q.get("display_logic"), f"question '{q['id']}' display_logic", qref, errors)
            for o in q.get("options", []):
                _walk_conditions(o.get("display_logic"), f"option '{q['id']}/{o.get('value')}' logic", qref, errors)
            for r in q.get("rows", []):
                _walk_conditions(r.get("display_logic"), f"row '{q['id']}/{r.get('id')}' logic", qref, errors)
            guards = _guards(q.get("display_logic")) | _guards(p.get("display_logic"))
            facing = dict(answer_key=answer_key, in_loop=in_loop, at_page=pi, q_page=q_page, q_rows=q_rows,
                          may_blank=may_blank, guards=guards, repeat_of=repeat_of)
            for field, text in (("title", q.get("title")), ("body", q.get("body"))):
                _check_tokens(text, f"question '{q['id']}' {field}", qids, url_vars, var_names, enrich_ids,
                              errors, **facing)
            def _own(node):          # an option/row label is also guarded by its own display logic
                return {**facing, "guards": guards | _guards(node)}
            for o in q.get("options", []):
                _check_tokens(str(o.get("label", "")), f"option '{q['id']}/{o.get('value')}' label", qids,
                              url_vars, var_names, enrich_ids, errors, **_own(o.get("display_logic")))
            for r in q.get("rows", []):
                _check_tokens(str(r.get("label", "")), f"row '{q['id']}/{r.get('id')}' label", qids,
                              url_vars, var_names, enrich_ids, errors, **_own(r.get("display_logic")))
            src = q.get("options_source")
            if (src and src.get("pipe_from") in qdefs and q.get("required")
                    and src["pipe_from"] not in screened):
                sq, mode = qdefs[src["pipe_from"]], src.get("mode", "selected")
                why = may_blank(src["pipe_from"])
                if not why and mode == "selected" and any(o.get("exclusive") for o in sq.get("options", [])):
                    why = "is answered with an exclusive option (e.g. 'None of these')"
                if not why and mode == "not_selected":
                    why = "has every option selected"
                if why:
                    errors.append(f"[warn] question '{q['id']}': its options are piped from "
                                  f"'{src['pipe_from']}', so it has none when '{src['pipe_from']}' {why} — "
                                  f"the question is then skipped. Fine if intended; otherwise make "
                                  f"'{src['pipe_from']}' required or add a screen-out for that case")
            if src and src.get("pipe_from") not in qids:
                errors.append(f"question '{q['id']}': options_source.pipe_from '{src.get('pipe_from')}' is unknown")
            elif src and q_page[src["pipe_from"]] >= pi:
                errors.append(f"question '{q['id']}': options_source.pipe_from '{src['pipe_from']}' is on "
                              f"{'this' if q_page[src['pipe_from']] == pi else 'a later'} page — its answer "
                              f"doesn't exist yet, so there'd be nothing to pipe. Pipe from an earlier page")
            elif src and src.get("pipe_from") in repeat_of:
                errors.append(f"question '{q['id']}': options_source.pipe_from '{src.get('pipe_from')}' is a "
                              f"repeated question — pipe from a plain question")
            if q.get("type") in ("video", "image") and not (q.get("media") or {}).get("url"):
                errors.append(f"question '{q['id']}' ({q['type']}): missing media.url")
            if q.get("type") in _CHOICE_TYPES and not q.get("options") and not q.get("options_source"):
                errors.append(f"question '{q['id']}' ({q['type']}): choice question has no options or options_source")
            if q.get("type") == "grid" and not q.get("rows"):
                errors.append(f"question '{q['id']}' (grid): grid question has no rows")

    for pi, p in enumerate(survey.get("pages", [])):
        _check_tokens(p.get("title"), f"page '{p['id']}' title", qids, url_vars, var_names, enrich_ids,
                      errors, answer_key, p.get("repeat") if isinstance(p.get("repeat"), dict) else None,
                      at_page=pi, q_page=q_page, q_rows=q_rows,
                      may_blank=may_blank, repeat_of=repeat_of)

    def check_tokens(text, where):   # actions run after a submit, so any page's answers can be read
        _check_tokens(text, where, qids, url_vars, var_names, enrich_ids, errors, answer_key, q_rows=q_rows)

    has_complete = False
    for i, a in enumerate(survey.get("actions", [])):
        t = a.get("type")
        _walk_conditions(a.get("when"), f"action[{i}] ({t}) when", check_ref, errors)
        if t == "skip" and a.get("to_page") not in page_ids:
            errors.append(f"action[{i}] skip -> unknown page '{a.get('to_page')}'")
        if t == "webhook":
            if not a.get("url"):
                errors.append(f"action[{i}] webhook -> missing 'url'")
            check_tokens(a.get("url"), f"action[{i}] webhook url")
            for k, v in (a.get("payload") or {}).items():
                if isinstance(v, str):          # nested objects are sent as-is, never piped
                    check_tokens(v, f"action[{i}] webhook payload['{k}']")
        if t == "set_variable":
            if not a.get("name"):
                errors.append(f"action[{i}] set_variable -> missing 'name'")
            if a.get("value_from"):
                msg = answer_key(a["value_from"], False)
                if msg:
                    errors.append(f"action[{i}] set_variable -> value_from {msg}")
        if t in ("complete", "disqualify"):
            redirect = a.get("redirect") or {}
            check_tokens(redirect.get("url"), f"action[{i}] {t} redirect url")
            check_tokens(redirect.get("task_url"), f"action[{i}] {t} redirect task_url")
        if t == "disqualify":
            check_tokens(a.get("dq_message"), f"action[{i}] disqualify dq_message")
        if t == "overquota":
            check_tokens(a.get("message"), f"action[{i}] overquota message")
        if t == "complete":
            has_complete = True
    if not has_complete:
        errors.append("no 'complete' action — survey has no terminal completion/redirect")

    for h in survey.get("hidden", []):
        where = f"hidden '{h.get('id')}'"
        _walk_conditions(h.get("when"), f"{where} when", check_ref, errors)
        if "calc" in h:
            _walk_calc(h["calc"], f"{where} calc", check_ref, errors)

    for qd in survey.get("quotas", []):
        if qd.get("scope") == "answer":
            msg = answer_key(qd.get("ref", {}).get("id"), False)
            if msg:
                errors.append(f"quota '{qd.get('id')}': ref {msg}")

    for p in survey.get("pages", []):
        for q in p.get("questions", []):
            v = q.get("validation") or {}
            if "regex" in v:
                problem = _safety.regex_problem(v["regex"])
                if problem:
                    errors.append(f"question '{q['id']}' validation.regex: {problem}")

    for i, e in enumerate(survey.get("settings", {}).get("enrichments", [])):
        if isinstance(e, dict):
            errors.extend(enrichment_spec_errors(e, f"settings.enrichments[{i}] '{e.get('id')}'"))

    for ac in survey.get("settings", {}).get("anti_fraud", {}).get("attention_checks", []):
        if ac not in qids:
            errors.append(f"anti_fraud.attention_checks: unknown question '{ac}'")

    return errors


def _criteria_errors(node, where, errors, depth=0):
    if not isinstance(node, dict) or _too_deep("criteria", where, depth, errors):
        return
    if "conditions" in node:
        for c in node.get("conditions") or []:
            _criteria_errors(c, where, errors, depth + 1)
        return
    if "field" in node and not _safety.is_identifier(node["field"]):
        errors.append(f"{where}: criteria field {node['field']!r} is not a plain identifier "
                      f"({_safety.IDENTIFIER_RE.pattern})")
    if node.get("operator") == "regex":
        problem = _safety.regex_problem(node.get("value"))
        if problem:
            errors.append(f"{where}: criteria regex {problem}")


def enrichment_spec_errors(spec, where="enrichment"):
    """Lint an inline behavioral enrichment spec. Its `field` and criteria fields become column
    names in a warehouse query, so they must be plain identifiers (values are always bound
    parameters). `source: llm` specs have no warehouse query and are skipped."""
    errors = []
    if spec.get("source") == "llm":
        return errors
    for key in ("event_type", "field"):
        if key in spec and spec[key] is not None and not _safety.is_identifier(spec[key]):
            errors.append(f"{where}: {key} {spec[key]!r} is not a plain identifier "
                          f"({_safety.IDENTIFIER_RE.pattern})")
    _criteria_errors(spec.get("criteria"), where, errors)
    return errors


def validate(spec):
    return schema_errors(spec) + semantic_errors(spec)


def _main():
    if len(sys.argv) < 2:
        print("usage: survey-agent validate <survey.json>")
        sys.exit(2)
    path = sys.argv[1]
    with open(path) as f:
        spec = json.load(f)
    errs = validate(spec)
    if errs:
        print(f"❌ {len(errs)} problem(s) in {path}:")
        for e in errs:
            print("   -", e)
        sys.exit(1)
    print(f"✅ {path} is valid (schema + semantic lint).")


if __name__ == "__main__":
    _main()
