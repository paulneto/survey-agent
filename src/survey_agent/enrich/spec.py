"""Behavioral enrichment spec — a declarative query over a respondent's behavioral events.

An enrichment is *data, not SQL*: it names an event_type, a time window, criteria, and an
operation. The provider turns it into either a warehouse query (FireboltProvider) or an in-Python
computation over cached/fixture events (StaticProvider / cache). Same spec, two executors.

    {
      "id": "stream_search_count_30d",
      "event_type": "web_search",              # -> a warehouse table (see event_types.json)
      "window": { "last": "30d" },             # or { "from": "2026-01-01", "to": "2026-02-01" }
      "criteria": {                             # condition tree over event fields (and/or)
        "op": "or",
        "conditions": [
          { "field": "search_term", "operator": "contains_any",
            "value": ["netflix", "disney", "max", "what to watch"] }
        ]
      },
      "operation": "count",                     # see OPERATIONS
      "field": "search_term",                   # field the operation acts on (sum/avg/latest/...)
      "type": "number"                          # result type hint (number|scalar|list|bool)
    }

OPERATIONS:
  count           number of matching events
  exists          bool: any matching event
  distinct_count  number of distinct values of `field`
  sum/avg/min/max numeric aggregate of `field` (e.g. sum minutes = time spent)
  latest/first    value of `field` on the most-recent / earliest matching event
  list            list of `field` values (matching events)
  top             most frequent value of `field`

CRITERIA operators (over event fields):
  equals, not_equals, contains, contains_any, in, not_in, gt, lt, gte, lte, between, regex, is_set

Field names (`field`, criteria `field`) must be plain identifiers — they become column names in the
compiled SQL (engine.validate lints them at the gate; enrich.firebolt_sql re-checks before building).

RESPONDENT ID COLUMN: the warehouse column holding the respondent id is `respondent_id` by default.
Override it per event type with `"id_field"` in the catalog, or globally with SURVEY_ENRICH_ID_FIELD.
"""
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta

from survey_agent.engine.safety import safe_search

OPERATIONS = {"count", "exists", "distinct_count", "sum", "avg", "min", "max",
              "latest", "first", "list", "top"}

# Catalog path is overridable so a deployment can supply its own warehouse schema without forking.
_CATALOG = os.environ.get("SURVEY_EVENT_TYPES") or os.path.join(os.path.dirname(__file__), "event_types.json")
with open(_CATALOG) as _f:
    EVENT_TYPES = {k: v for k, v in json.load(_f).items() if not k.startswith("_")}

ID_FIELD = os.environ.get("SURVEY_ENRICH_ID_FIELD") or "respondent_id"


def id_field_for(event_type):
    """The warehouse column that holds the respondent id for this event type."""
    return (EVENT_TYPES.get(event_type) or {}).get("id_field") or ID_FIELD


# ----------------------------------------------------------------- windows
def window_start(window, now):
    """Return the inclusive lower bound datetime for a window, or None if open-ended."""
    if not window:
        return None
    if "last" in window:
        return now - _parse_duration(window["last"])
    if "from" in window:
        return _parse_dt(window["from"])
    return None


def window_end(window):
    if window and "to" in window:
        return _parse_dt(window["to"])
    return None


def _parse_duration(s):
    m = re.fullmatch(r"(\d+)\s*(h|d|w|mo)", str(s).strip())
    if not m:
        raise ValueError(f"bad window duration: {s!r} (use e.g. 24h, 7d, 4w, 3mo)")
    n, unit = int(m.group(1)), m.group(2)
    return {"h": timedelta(hours=n), "d": timedelta(days=n),
            "w": timedelta(weeks=n), "mo": timedelta(days=30 * n)}[unit]


def _parse_dt(s):
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


# ----------------------------------------------------------------- criteria
def _match(node, event):
    if not node:
        return True
    if "conditions" in node:
        results = [_match(c, event) for c in node["conditions"]]
        return all(results) if node.get("op", "and") == "and" else any(results)
    field, op, val = node["field"], node["operator"], node.get("value")
    actual = event.get(field)
    if op == "is_set":
        return actual not in (None, "")
    if actual is None:
        return op in ("not_equals", "not_in")
    a = str(actual).lower()
    if op == "equals":
        return str(actual) == str(val)
    if op == "not_equals":
        return str(actual) != str(val)
    if op == "contains":
        return str(val).lower() in a
    if op == "contains_any":
        return any(str(v).lower() in a for v in val)
    if op == "in":
        return str(actual) in {str(v) for v in val}
    if op == "not_in":
        return str(actual) not in {str(v) for v in val}
    if op in ("gt", "lt", "gte", "lte"):
        try:
            x, y = float(actual), float(val)
        except (TypeError, ValueError):
            return False
        return {"gt": x > y, "lt": x < y, "gte": x >= y, "lte": x <= y}[op]
    if op == "between":
        try:
            x = float(actual)
            return float(val[0]) <= x <= float(val[1])
        except (TypeError, ValueError):
            return False
    if op == "regex":
        return bool(safe_search(val, actual))  # bounded; refused/malformed patterns fail closed
    raise ValueError(f"unknown criteria operator: {op!r}")


# ----------------------------------------------------------------- evaluate
def evaluate(spec, events, now):
    """Compute the enrichment value over a list of event dicts (StaticProvider / cache path)."""
    et = EVENT_TYPES.get(spec["event_type"], {})
    tf = et.get("time_field", "event_time")
    lo = window_start(spec.get("window"), now)
    hi = window_end(spec.get("window"))

    def in_window(e):
        if lo is None and hi is None:
            return True
        try:
            t = _parse_dt(e.get(tf))
        except (TypeError, ValueError):
            return True
        return (lo is None or t >= lo) and (hi is None or t <= hi)

    matched = [e for e in events if in_window(e) and _match(spec.get("criteria"), e)]
    op = spec["operation"]
    field = spec.get("field")
    if op == "count":
        return len(matched)
    if op == "exists":
        return len(matched) > 0
    vals = [e.get(field) for e in matched if field and e.get(field) is not None]
    nums = [float(v) for v in vals if _is_number(v)]
    if op == "distinct_count":
        return len(set(map(str, vals)))
    if op == "sum":
        return round(sum(nums), 3)
    if op == "avg":
        return round(sum(nums) / len(nums), 3) if nums else None
    if op == "min":
        return min(nums) if nums else None
    if op == "max":
        return max(nums) if nums else None
    if op == "list":
        return vals
    if op == "top":
        return Counter(map(str, vals)).most_common(1)[0][0] if vals else None
    if op in ("latest", "first"):
        ordered = sorted(matched, key=lambda e: str(e.get(tf, "")), reverse=(op == "latest"))
        return ordered[0].get(field) if ordered else None
    raise ValueError(f"unknown operation: {op!r}")


def _is_number(v):
    try:
        float(v)
        return not isinstance(v, bool)
    except (TypeError, ValueError):
        return False
