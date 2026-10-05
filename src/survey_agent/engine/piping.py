"""Piping — merge tokens and option piping. Always resolved at render time, never stored.

Merge tokens (inside any title / option label / dq_message / redirect URL):
  {{url:uid}}              -> a captured URL variable
  {{var:segment}}          -> a set_variable / hidden value
  {{ctx:fact}}             -> a context (enrichment) fact; {{behavior:…}} / {{enrich:…}} are aliases
  {{q:q_brand.answer}}     -> a prior answer's display label(s)
  {{q:q_brand.answer.value}} or {{q:q_brand.value}} -> the stored value(s)
  {{q:q_grid.row:r_taste.answer}} -> a grid cell
  {{q:q_rate[netflix].answer}}     -> one repeat instance's answer; `[@]` = the current repeat item
  {{loop:label}} / {{loop:value}} / {{loop:index}} / {{loop:total}} -> the current repeat item
Fallback: `{{q:q_fav.answer|your favourite}}` renders the text after `|` when the value is blank
(an optional question left empty, a skipped page, a URL variable missing from the link).
Unknown / unresolvable tokens render as "" (or the fallback) — we never leak raw {{...}} to a
respondent. `validate` rejects unknown token kinds and fields so that doesn't happen by accident.

Option piping (a question whose options are derived from a prior question's answers):
  "options_source": {"pipe_from": "q_brands", "mode": "selected"|"not_selected"|"all"}
Exclusive options ("None of these") are not piped in `selected`/`not_selected` mode. A choice
question left with no options is skipped by the runtime rather than shown empty.
"""
import re

from .repeat import bind_current

TOKEN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
KINDS = ("q", "url", "var", "ctx", "behavior", "enrich", "loop")
ENRICH_KINDS = ("ctx", "behavior", "enrich")
Q_FIELDS = ("answer", "value", "answer.value")


def split_token(tok):
    """'q:q_fav.answer|your favourite' -> ('q', 'q_fav.answer', 'your favourite' | None)."""
    body, bar, fallback = tok.partition("|")
    kind, _, rest = body.strip().partition(":")
    return kind.strip(), rest.strip(), (fallback.strip() if bar else None)


def parse_q(rest):
    """'q_grid.row:r1.value' -> ('q_grid', 'r1', 'value'); 'q_fav' -> ('q_fav', None, 'answer')."""
    row = None
    if ".row:" in rest:
        qid, tail = rest.split(".row:", 1)
        row, _, field = tail.partition(".")
    else:
        qid, _, field = rest.partition(".")
    return qid, row, field or "answer"


def build_label_lookup(definition):
    """question_id -> {str(option_value): label} for label piping.

    A question with piped options (`options_source`) has no static `options` of its own, so it
    inherits the value→label map of its `pipe_from` source question. The source always sits on an
    earlier page (server-driven flow), so it is already in `lut` by the time we reach the piped
    question — this keeps piped answers labelized in both piping tokens and export, like any other.
    """
    lut = {}
    for page in definition.get("pages", []):
        for q in page.get("questions", []):
            own = {
                str(o.get("value")): o.get("label", o.get("value"))
                for o in q.get("options", [])
            }
            src = q.get("options_source")
            if not own and isinstance(src, dict) and src.get("pipe_from") in lut:
                own = dict(lut[src["pipe_from"]])
            lut[q["id"]] = own
    return lut


def _labelize(qid, value, lut):
    opts = lut.get(qid, {})
    if isinstance(value, list):
        return ", ".join(str(opts.get(str(v), v)) for v in value)
    return str(opts.get(str(value), value))


def _value(kind, rest, ctx, lut):
    if kind == "url":
        return str(ctx.url_vars.get(rest, "") or "")
    if kind == "var":
        v = ctx.variables.get(rest, "")
        return ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
    if kind in ENRICH_KINDS:
        v = ctx.enrichments.get(rest, "")
        return ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
    if kind == "loop":
        v = (getattr(ctx, "loop", None) or {}).get(rest, "")
        return "" if v is None else str(v)
    if kind == "q":
        qid, row, field = parse_q(rest)
        qid = bind_current(qid, getattr(ctx, "loop", None))
        if qid is None:
            return ""
        val = ctx.answers.get(qid)
        if row is not None:
            val = val.get(row) if isinstance(val, dict) else None
        if val is None or val == "" or val == []:
            return ""
        if field in ("value", "answer.value"):
            return ", ".join(map(str, val)) if isinstance(val, list) else str(val)
        return _labelize(qid, val, lut)
    return ""


def _resolve_token(tok, ctx, lut):
    kind, rest, fallback = split_token(tok)
    v = _value(kind, rest, ctx, lut)
    return fallback if v == "" and fallback is not None else v


def blank_tokens(text, ctx, lut):
    """Tokens in `text` that render empty for this respondent and have no fallback — what a
    respondent would see as a gap ("Why do you love ?"). The simulator reports these."""
    if not text or "{{" not in str(text):
        return []
    return [m.group(0) for m in TOKEN.finditer(str(text))
            if split_token(m.group(1))[2] is None and _resolve_token(m.group(1), ctx, lut) == ""]


_MD_SPECIAL = re.compile(r"([\\`*_\[\]()!#>-])")


def md_escape(s):
    """Backslash-escape Markdown so piped text renders literally (render.md_inline honours these)."""
    return _MD_SPECIAL.sub(r"\\\1", s)


def _untrusted(kind, rest, ctx, lut):
    """Does this token pipe text the survey author didn't write? A respondent's typed answer, a URL
    value, a variable or a context fact can contain anything; an option label (`{{q:…}}` on a choice
    question, `{{loop:…}}`) is authored, so its own Markdown should still render."""
    if kind == "loop":
        return False
    if kind == "q":
        qid = bind_current(parse_q(rest)[0], getattr(ctx, "loop", None))
        return not (qid and lut.get(qid))
    return True


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def strip_links(s):
    """`[Claim $500](https://…)` -> `Claim $500`; `![alt](https://…)` -> `alt`. Applied to values the
    author didn't write when they're shown to a respondent, so link syntax neither works nor shows."""
    return _MD_LINK.sub(lambda m: m.group(1), s)


def resolve_text(text, ctx, lut, markdown=False, display=False):
    """Pipe every token in `text`. `markdown=True` gives the copy a Markdown renderer should use: piped
    values the author didn't write are backslash-escaped so they can't inject a link or image, while
    authored text, option labels and fallbacks keep their formatting. The engine returns plain text
    as `title`/`body`/`label` and this copy as `title_md`/`body_md`/`label_md` (only when it differs),
    so chat and other non-HTML consumers never see the backslashes. `display=True` (text shown to a
    respondent) also turns link/image syntax in those values into its plain words. Never set either
    for redirect URLs, webhook payloads or exports, which need the value exactly as received."""
    if not text or "{{" not in text:
        return text

    def sub(m):
        kind, rest, fallback = split_token(m.group(1))
        v = _value(kind, rest, ctx, lut)
        if v == "" and fallback is not None:
            return fallback
        if not _untrusted(kind, rest, ctx, lut):
            return v
        if display:                              # on screen: drop link syntax, keep its words
            v = strip_links(v)
        return md_escape(v) if markdown else v
    return TOKEN.sub(sub, text)


def resolve_options_source(question, ctx, source_options):
    """Materialize a question's options from a prior question's answers (option piping).

    source_options is the option list of the pipe_from question (preserving original
    value/label so cross-references and export stay stable).
    """
    src = question.get("options_source")
    if not src:
        return question.get("options", [])
    selected = ctx.answers.get(src["pipe_from"])
    if isinstance(selected, list):
        sel = set(map(str, selected))
    elif selected in (None, ""):
        sel = set()
    else:
        sel = {str(selected)}
    mode = src.get("mode", "selected")
    out = []
    for o in source_options:
        if o.get("exclusive") and mode != "all":   # never pipe "None of these" forward as a choice
            continue
        v = str(o.get("value"))
        keep = (
            mode == "all"
            or (mode == "selected" and v in sel)
            or (mode == "not_selected" and v not in sel)
        )
        if keep:
            out.append(o)
    return out
