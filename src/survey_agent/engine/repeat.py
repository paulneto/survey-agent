"""Repeating — ask a question, or a whole page, once per item from an earlier answer.

A `repeat` block is data, like logic:

  "repeat": {"source": "q_brands",          # an earlier question (on an earlier page)
             "mode": "selected",            # selected (default) | not_selected
             "by": "answers",               # answers (default) | value — a number N → N loops
             "exclude": ["other"],          # option values never looped over
             "max": 20,                     # cap on iterations (default 50, hard cap 150)
             "randomize": false}            # seeded per-session order (default: source order)

On a **page**, the whole page is shown once per item. On a **question**, the question is shown once
per item, all on the same page (not allowed on a question inside a repeated page). Exclusive options ("none of the above") are never looped over.

Every repeated answer is stored under its own **instance id** `qid[item]` (e.g. `q_rate[netflix]`),
so it is an ordinary answer everywhere else: logic refs (`{"kind": "question", "id": "q_rate[netflix]"}`),
merge tokens (`{{q:q_rate[netflix].answer}}`), quotas and export all work unchanged. Authored ids
can't contain brackets (schema pattern), so an instance id never collides with a real question.

Inside a repeat, the current item is available as:
  merge tokens   {{loop:label}} {{loop:value}} {{loop:index}} {{loop:total}}
  logic ref      {"kind": "loop", "id": "value"|"label"|"index"|"total"}
  `[@]`          `q_aware[@]` in a ref or token means "this item's answer to q_aware" — so a later
                 repeat over the same items can follow up only where an earlier one said yes.
"""
import re

INSTANCE = re.compile(r"^(?P<base>[A-Za-z0-9_-]+)\[(?P<item>[^\[\]]*)\]$")
# Item values become part of an instance id and a merge token, so they must be token-safe.
SAFE_ITEM = re.compile(r"^[A-Za-z0-9_-]+$")
CURRENT = "@"
DEFAULT_MAX = 50
HARD_MAX = 150
LOOP_FIELDS = ("value", "label", "index", "total")


def instance_id(base, item):
    return f"{base}[{item}]"


def split_instance(key):
    """'q_rate[netflix]' -> ('q_rate', 'netflix'); a plain id -> (id, None)."""
    m = INSTANCE.match(str(key))
    return (m["base"], m["item"]) if m else (key, None)


def bind_current(key, loop):
    """Replace a trailing `[@]` with the current loop item; None if there's no loop to bind to."""
    base, item = split_instance(key)
    if item != CURRENT:
        return key
    return instance_id(base, loop["value"]) if loop else None


def cap(spec):
    try:
        n = int(spec.get("max", DEFAULT_MAX))
    except (TypeError, ValueError):
        n = DEFAULT_MAX
    return max(0, min(n, HARD_MAX))


def source_options(qid, questions_by_id, _seen=None):
    """The option list a repeat iterates over. A source whose own options are piped
    (`options_source`) inherits them from its `pipe_from` question."""
    q = questions_by_id.get(qid) or {}
    if q.get("options"):
        return q["options"]
    src = q.get("options_source")
    seen = _seen or set()
    if isinstance(src, dict) and src.get("pipe_from") and qid not in seen:
        return source_options(src["pipe_from"], questions_by_id, seen | {qid})
    return []


def items(spec, answers, questions_by_id):
    """[{value, label}] the repeat iterates over, in source-option order (before randomization).
    Labels are raw (may hold merge tokens); the runtime pipes them at render time."""
    ans = answers.get(spec.get("source"))
    limit = cap(spec)
    if spec.get("by") == "value":
        try:
            n = int(float(ans))
        except (TypeError, ValueError, OverflowError):   # blank, text, inf, nan
            n = 0
        return [{"value": str(i), "label": str(i)} for i in range(1, max(0, min(n, limit)) + 1)]
    if isinstance(ans, list):
        sel = set(map(str, ans))
    elif ans in (None, ""):
        sel = set()
    else:
        sel = {str(ans)}
    want_selected = spec.get("mode", "selected") == "selected"
    exclude = set(map(str, spec.get("exclude", [])))
    out = []
    for o in source_options(spec.get("source"), questions_by_id):
        v = str(o.get("value"))
        if o.get("exclusive") or v in exclude:
            continue
        if (v in sel) == want_selected:
            out.append({"value": v, "label": str(o.get("label", v))})
    return out[:limit]


class InstanceIndex(dict):
    """A {question_id: X} map that also answers for instance ids: `q_rate[netflix]` resolves to the
    entry for `q_rate`. Lets every existing `questions_by_id.get(qid)` / `label_lut.get(qid)` caller
    handle repeated answers without knowing about repeating."""

    def _base(self, key):
        if dict.__contains__(self, key):
            return key
        base, item = split_instance(key)
        return base if item is not None and dict.__contains__(self, base) else None

    def get(self, key, default=None):
        b = self._base(key)
        return dict.__getitem__(self, b) if b is not None else default

    def __missing__(self, key):
        b = self._base(key)
        if b is None:
            raise KeyError(key)
        return dict.__getitem__(self, b)

    def __contains__(self, key):
        return self._base(key) is not None
