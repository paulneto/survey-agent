"""Compile a behavioral enrichment/trigger spec into parameterized Firebolt SQL.

Two entry points:
  compile_value_sql(spec, uid)        -> (sql, params)  one respondent's enrichment value
  compile_detect_sql(spec, since=...) -> (sql, params)  distinct respondent ids matching
                                                          (used by the trigger sidecar, incremental)

The spec is the same declarative shape used everywhere (event_type + window + criteria +
operation). Producing SQL from data (not letting an LLM write SQL) keeps it safe and reviewable:

* every VALUE is a bound parameter — named placeholders (%(name)s); adapt to the driver's
  paramstyle on execute;
* every IDENTIFIER (criteria `field`, the spec's `field`, the catalog's `table` / `time_field` /
  `id_field`) is checked against a strict identifier pattern (engine/safety.py) and, when the
  event type's catalog entry lists `fields`, against that allow-list — anything else raises
  ValueError before any SQL is built.

The respondent id column defaults to `respondent_id`; set `id_field` on an event type in the
catalog, or SURVEY_ENRICH_ID_FIELD, to match your warehouse.
"""
import re

from survey_agent.engine.safety import require_identifier, require_table_name

from .spec import EVENT_TYPES, id_field_for


def _event_type(spec):
    et_name = spec.get("event_type")
    if et_name not in EVENT_TYPES:
        raise ValueError(f"unknown event_type: {et_name!r}")
    return et_name, EVENT_TYPES[et_name]


def _allowed_fields(et):
    """Column allow-list for an event type, or None when the catalog doesn't declare `fields`."""
    if not et.get("fields"):
        return None
    allowed = set(et["fields"])
    for k in ("duration_field", "time_field", "id_field"):
        if et.get(k):
            allowed.add(et[k])
    return allowed


def _column(name, et, what="field"):
    require_identifier(name, what)
    allowed = _allowed_fields(et)
    if allowed is not None and name not in allowed:
        raise ValueError(f"{what} {name!r} is not a declared field of this event type "
                         f"(allowed: {', '.join(sorted(allowed))})")
    return name


def _criteria_sql(node, params, et, prefix="c"):
    if not node:
        return "1=1"
    if "conditions" in node:
        joiner = " AND " if node.get("op", "and") == "and" else " OR "
        parts = [_criteria_sql(c, params, et, f"{prefix}{i}") for i, c in enumerate(node["conditions"])]
        return "(" + joiner.join(parts) + ")" if parts else "1=1"
    field = _column(node.get("field"), et, "criteria field")
    op, val = node["operator"], node.get("value")
    p = prefix
    if op in ("equals", "not_equals"):
        params[p] = val
        return f"{field} {'=' if op == 'equals' else '!='} %({p})s"
    if op == "contains":
        params[p] = f"%{val}%"
        return f"LOWER({field}) LIKE LOWER(%({p})s)"
    if op == "contains_any":
        clauses = []
        for i, v in enumerate(val):
            params[f"{p}_{i}"] = f"%{v}%"
            clauses.append(f"LOWER({field}) LIKE LOWER(%({p}_{i})s)")
        return "(" + " OR ".join(clauses) + ")"
    if op in ("in", "not_in"):
        names = []
        for i, v in enumerate(val):
            params[f"{p}_{i}"] = v
            names.append(f"%({p}_{i})s")
        return f"{field} {'NOT ' if op == 'not_in' else ''}IN ({', '.join(names)})"
    if op in ("gt", "lt", "gte", "lte"):
        params[p] = val
        sql_op = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}[op]
        return f"{field} {sql_op} %({p})s"
    if op == "between":
        params[f"{p}_lo"], params[f"{p}_hi"] = val[0], val[1]
        return f"{field} BETWEEN %({p}_lo)s AND %({p}_hi)s"
    if op == "regex":
        params[p] = val
        return f"{field} ~ %({p})s"
    if op == "is_set":
        return f"{field} IS NOT NULL"
    raise ValueError(f"unknown criteria operator: {op!r}")


_UNITS = {"h": "HOUR", "d": "DAY", "w": "WEEK", "mo": "MONTH"}


def _window_sql(spec, params, tf):
    w = spec.get("window") or {}
    if "last" in w:
        m = re.fullmatch(r"\s*(\d+)\s*(h|d|w|mo)\s*", str(w["last"]))
        if not m:
            raise ValueError(f"bad window duration: {w['last']!r} (use e.g. 24h, 7d, 4w, 3mo)")
        return f"{tf} >= NOW() - INTERVAL '{int(m.group(1))}' {_UNITS[m.group(2)]}"
    if "from" in w:
        params["win_from"] = w["from"]
        clause = f"{tf} >= %(win_from)s"
        if "to" in w:
            params["win_to"] = w["to"]
            clause += f" AND {tf} <= %(win_to)s"
        return clause
    return "1=1"


def _select_expr(op, field):
    return {
        "count": "COUNT(*)",
        "exists": "COUNT(*) > 0",
        "distinct_count": f"COUNT(DISTINCT {field})",
        "sum": f"COALESCE(SUM({field}), 0)",
        "avg": f"AVG({field})",
        "min": f"MIN({field})",
        "max": f"MAX({field})",
        "list": f"ARRAY_AGG({field})",
    }.get(op)


def _names(spec):
    et_name, et = _event_type(spec)
    table = require_table_name(et.get("table"), "catalog table")
    tf = require_identifier(et.get("time_field") or "event_time", "catalog time_field")
    idf = require_identifier(id_field_for(et_name), "respondent id field")
    return et, table, tf, idf


def compile_value_sql(spec, uid):
    """SQL for one respondent's enrichment value."""
    et, table, tf, idf = _names(spec)
    params = {"uid": uid}
    where = (f"{idf} = %(uid)s AND {_window_sql(spec, params, tf)} AND "
             f"{_criteria_sql(spec.get('criteria'), params, et)}")
    op = spec["operation"]
    field = spec.get("field")
    if op in ("latest", "first", "top", "distinct_count", "sum", "avg", "min", "max", "list"):
        field = _column(field, et, "field")
    if op in ("latest", "first"):
        order = "DESC" if op == "latest" else "ASC"
        sql = f"SELECT {field} FROM {table} WHERE {where} ORDER BY {tf} {order} LIMIT 1"
    elif op == "top":
        sql = f"SELECT {field} FROM {table} WHERE {where} GROUP BY {field} ORDER BY COUNT(*) DESC LIMIT 1"
    else:
        expr = _select_expr(op, field)
        if expr is None:
            raise ValueError(f"unknown operation: {op!r}")
        sql = f"SELECT {expr} FROM {table} WHERE {where}"
    return sql, params


def compile_detect_sql(spec, since=None, limit=10000):
    """Distinct respondent ids whose behavior matches — for the trigger sidecar.
    `since` is an event_time watermark for incremental polling."""
    et, table, tf, idf = _names(spec)
    params = {}
    clauses = [_window_sql(spec, params, tf), _criteria_sql(spec.get("criteria"), params, et)]
    if since is not None:
        params["since"] = since
        clauses.append(f"{tf} > %(since)s")
    sql = (f"SELECT DISTINCT {idf}, MAX({tf}) AS last_event "
           f"FROM {table} WHERE {' AND '.join(clauses)} GROUP BY {idf} LIMIT {int(limit)}")
    return sql, params
