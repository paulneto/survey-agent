"""Condition AST evaluator — the single, pure logic engine reused everywhere.

A condition is *data*, never code: there is no eval()/exec(), so an LLM can author logic
safely and it is validated against JSON Schema at publish time. The same evaluator powers
question/page/option/grid-row display logic AND skip/disqualify/overquota action `when`
clauses — one implementation, used in every place a commercial survey platform has a separate logic surface.

Node forms (mutually recursive):
  ConditionGroup : {"op": "and"|"or", "conditions": [node, ...]}
  Condition      : {"ref": {...}, "operator": "<op>", "value": <any>}
  empty / None   : always True — defensive, for internal callers. NOT an authoring form: the
                   JSON Schema REJECTS a literal `"display_logic": {}`. To mean "always show",
                   omit the key entirely.

Reference kinds (ref.kind) — the schema enum is the authority (survey.schema.json $defs.ref):
  question   : a prior answer (by question id); optional "row" for grid cells
  url_var    : a captured inbound URL variable (uid, rid, group, lang, ...)
  variable   : a value set by a set_variable action (or a hidden value)
  quota      : live quota state {count, limit} — use with operator is_full / is_not_full
  enrichment : a behavioral fact resolved by the configured SURVEY_ENRICH adapter
  loop       : the current repeat item (id = value | label | index | total) — only inside a repeat
               (engine/repeat.py). A question ref id ending `[@]` binds to the current item too.

Blank-answer convention: a comparison against an unanswered/blank value is False (logic
only fires when there is data), except is_answered (False) / is_not_answered (True).
"""
from numbers import Number

from . import safety
from .repeat import bind_current

COMPARISON_OPERATORS = {
    "equals", "equals_ordered", "not_equals", "contains", "is_one_of", "is_not_one_of",
    "is_answered", "is_not_answered", "greater_than", "less_than",
    "greater_or_equal", "less_or_equal", "between", "regex_match",
    "is_full", "is_not_full",
}


class EvalContext:
    """The state a condition is evaluated against."""

    def __init__(self, answers=None, url_vars=None, variables=None, quotas=None, enrichments=None,
                 loop=None):
        self.answers = answers or {}          # question_id -> scalar | list | {row_id: value}
        self.url_vars = url_vars or {}        # name -> str
        self.variables = variables or {}      # name -> value
        self.quotas = quotas or {}            # quota_id -> {"count": int, "limit": int|None}
        self.enrichments = enrichments or {}  # enrichment_id -> behavioral fact (from the warehouse/cache)
        self.loop = loop                      # current repeat item {value,label,index,total} | None

    def with_loop(self, loop):
        """Same state, bound to one repeat item."""
        return EvalContext(self.answers, self.url_vars, self.variables, self.quotas,
                           self.enrichments, loop)


def _is_blank(v):
    return v is None or v == "" or v == [] or v == {}


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _to_number(x):
    if isinstance(x, bool):
        raise ValueError("bool is not a number")
    if isinstance(x, Number):
        return x
    return float(str(x).strip())


def resolve_ref(ref, ctx):
    kind = ref.get("kind")
    rid = ref.get("id")
    if kind == "question":
        rid = bind_current(rid, ctx.loop)
        if rid is None:                       # `q[@]` outside a repeat: nothing to bind to
            return None
        val = ctx.answers.get(rid)
        row = ref.get("row")
        if row is not None and isinstance(val, dict):
            return val.get(row)
        return val
    if kind == "url_var":
        return ctx.url_vars.get(rid)
    if kind == "variable":
        return ctx.variables.get(rid)
    if kind == "quota":
        return ctx.quotas.get(rid)
    if kind == "enrichment":
        return ctx.enrichments.get(rid)
    if kind == "loop":
        return (ctx.loop or {}).get(rid)
    raise ValueError(f"unknown ref kind: {kind!r}")


def _equals(actual, expected):
    if isinstance(actual, list):
        return set(map(str, actual)) == set(map(str, _as_list(expected)))
    return str(actual) == str(expected)


def _equals_ordered(actual, expected):
    """Order- and duplicate-sensitive equality — use on ranking answers, where [a,b,c] != [c,b,a].
    (`equals` is set-based, which is correct for checkbox/multi-select but wrong for a ranking.)"""
    if isinstance(actual, list):
        return list(map(str, actual)) == list(map(str, _as_list(expected)))
    return str(actual) == str(expected)


def _contains(actual, expected):
    if isinstance(actual, list):
        return str(expected) in set(map(str, actual))
    return str(expected) in str(actual)


def _is_one_of(actual, expected):
    exp = set(map(str, _as_list(expected)))
    if isinstance(actual, list):
        return bool(set(map(str, actual)) & exp)
    return str(actual) in exp


def _num(actual, expected, fn):
    try:
        return fn(_to_number(actual), _to_number(expected))
    except (ValueError, TypeError):
        return False


def eval_condition(cond, ctx):
    operator = cond["operator"]
    ref = cond["ref"]
    actual = resolve_ref(ref, ctx)

    # quota state is special: {count, limit}
    if ref.get("kind") == "quota":
        counter = actual or {}
        count = counter.get("count", 0)
        limit = counter.get("limit")
        if operator == "is_full":
            return limit is not None and count >= limit
        if operator == "is_not_full":
            return limit is None or count < limit
        actual = count  # allow numeric comparisons on the count

    if operator == "is_answered":
        return not _is_blank(actual)
    if operator == "is_not_answered":
        return _is_blank(actual)

    if _is_blank(actual):
        return False

    expected = cond.get("value")
    if operator == "equals":
        return _equals(actual, expected)
    if operator == "equals_ordered":
        return _equals_ordered(actual, expected)
    if operator == "not_equals":
        return not _equals(actual, expected)
    if operator == "contains":
        return _contains(actual, expected)
    if operator == "is_one_of":
        return _is_one_of(actual, expected)
    if operator == "is_not_one_of":
        return not _is_one_of(actual, expected)
    if operator == "greater_than":
        return _num(actual, expected, lambda a, b: a > b)
    if operator == "less_than":
        return _num(actual, expected, lambda a, b: a < b)
    if operator == "greater_or_equal":
        return _num(actual, expected, lambda a, b: a >= b)
    if operator == "less_or_equal":
        return _num(actual, expected, lambda a, b: a <= b)
    if operator == "between":
        if not isinstance(expected, (list, tuple)) or len(expected) != 2:
            return False          # fail closed on a malformed bound, like regex_match does
        lo, hi = expected
        return _num(actual, lo, lambda a, b: a >= b) and _num(actual, hi, lambda a, b: a <= b)
    if operator == "regex_match":
        # bounded: refuses over-long / catastrophic patterns and caps the input (engine/safety.py);
        # a refused or malformed pattern fails closed.
        return bool(safety.safe_search(expected, actual))
    raise ValueError(f"unknown operator: {operator!r}")


def evaluate(node, ctx):
    """Evaluate a condition node (group / condition / empty) → bool."""
    if not node:
        return True
    if "conditions" in node and "op" in node:
        results = (evaluate(c, ctx) for c in node["conditions"])
        op = node["op"]
        if op == "and":
            return all(results)
        if op == "or":
            return any(results)
        raise ValueError(f"unknown group op: {op!r}")
    if "ref" in node and "operator" in node:
        return eval_condition(node, ctx)
    raise ValueError(f"malformed condition node: {node!r}")
