"""Calculation AST — bounded, declarative arithmetic/scoring over answers, url-vars, variables, and
enrichment. Same philosophy as the logic AST: it's DATA, not code (no eval), so an LLM can author
hidden calculations safely. Used by `hidden` values (engine/runtime.py).

Node forms:
  {"op": "const", "value": X}
  {"op": "ref", "ref": {"kind": "question|variable|url_var|enrichment", "id": "..."}}
  {"op": "add|sub|mul|div|min|max|avg|sum", "args": [node, ...]}
  {"op": "count_selected", "ref": {...}}                 # length of a multi-select answer
  {"op": "score", "ref": {...}, "map": {"opt": n, ...}}  # map answer(s) to numbers (multi = summed)
  {"op": "bucket", "of": node, "ranges": [[lo, hi, label], ...], "default": "..."}
  {"op": "concat", "args": [node, ...], "sep": " "}
  {"op": "round", "of": node, "places": 0}
"""
from .ast_eval import resolve_ref


def _num(v):
    try:
        if isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _whole(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def evaluate(node, ctx):
    if not isinstance(node, dict):
        return node
    op = node.get("op")

    if op == "const":
        return node.get("value")
    if op == "ref":
        return resolve_ref(node["ref"], ctx)

    if op in ("add", "sum", "sub", "mul", "div", "min", "max", "avg"):
        vals = [_num(evaluate(a, ctx)) for a in node.get("args", [])]
        vals = [v for v in vals if v is not None]
        if not vals:
            return 0 if op in ("add", "sum") else None
        if op in ("add", "sum"):
            return _whole(sum(vals))
        if op == "sub":
            return _whole(vals[0] - sum(vals[1:]))
        if op == "mul":
            r = 1.0
            for v in vals:
                r *= v
            return _whole(r)
        if op == "div":
            return _whole(vals[0] / vals[1]) if len(vals) > 1 and vals[1] else None
        if op == "min":
            return _whole(min(vals))
        if op == "max":
            return _whole(max(vals))
        if op == "avg":
            return _whole(sum(vals) / len(vals))

    if op == "count_selected":
        v = resolve_ref(node["ref"], ctx)
        if isinstance(v, list):
            return len(v)
        return 1 if v not in (None, "") else 0

    if op == "score":
        v = resolve_ref(node["ref"], ctx)
        m = node.get("map", {})
        if isinstance(v, list):
            return _whole(sum(_num(m.get(str(x))) or 0 for x in v))
        return _whole(_num(m.get(str(v))) or 0)

    if op == "bucket":
        x = _num(evaluate(node["of"], ctx))
        if x is None:
            return node.get("default")
        for lo, hi, label in node.get("ranges", []):
            if (lo is None or x >= lo) and (hi is None or x <= hi):
                return label
        return node.get("default")

    if op == "concat":
        return node.get("sep", "").join(str(evaluate(a, ctx)) for a in node.get("args", []))

    if op == "round":
        v = _num(evaluate(node["of"], ctx))
        return None if v is None else _whole(round(v, node.get("places", 0)))

    raise ValueError(f"unknown calc op: {op!r}")
