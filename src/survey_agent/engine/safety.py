"""Guards for survey-authored strings that reach an interpreter: SQL identifiers and regexes.

Specs are authored by an LLM (or a person) and are untrusted input to the engine. Two places turn
author-supplied text into something executable:

* **SQL identifiers** — an enrichment spec's `field` / criteria `field` name a column in a
  warehouse query. Values are always bound parameters, but identifiers can't be, so they must
  match `IDENTIFIER_RE` (a plain unquoted identifier; `TABLE_RE` also allows `schema.table`).
* **Regular expressions** — `regex_match` (logic) and `validation.regex` (question input) run an
  author pattern against respondent-typed text. The real bound is execution time: every match runs
  on the `regex` engine with a hard `REGEX_TIMEOUT_SECONDS` timeout, so no accepted pattern can
  monopolize the serving process; a match that times out counts as "refused". The validator also
  rejects the shapes that backtrack catastrophically — nested unbounded quantifiers (`(x+)+`) and
  alternation inside a repeated group (`(a|aa)+`) — patterns over `MAX_PATTERN_LENGTH` chars, and
  the matched input is capped at `MAX_REGEX_INPUT` characters.

No I/O — safe to import from the engine. (`regex` is a core dependency; if it's somehow missing we
fall back to `re` with the static checks only.)
"""
import functools
import re

try:
    import regex as _regex
except ImportError:  # pragma: no cover - `regex` is a declared dependency
    _regex = None

try:  # Python 3.11+
    import re._parser as _sre_parse  # type: ignore[import-not-found]
    from re._constants import MAXREPEAT as _MAXREPEAT  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - older Pythons
    import sre_parse as _sre_parse  # type: ignore[no-redef]
    from sre_constants import MAXREPEAT as _MAXREPEAT  # type: ignore[no-redef]

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

MAX_PATTERN_LENGTH = 200
MAX_REGEX_INPUT = 2000
REGEX_TIMEOUT_SECONDS = 0.05
# A repeat whose upper bound exceeds this counts as "unbounded" for the nesting check.
_LARGE_REPEAT = 50


def is_identifier(name):
    return isinstance(name, str) and bool(IDENTIFIER_RE.match(name))


def is_table_name(name):
    return isinstance(name, str) and bool(TABLE_RE.match(name))


def require_identifier(name, what="identifier"):
    """Return `name` if it is a plain SQL identifier, else raise ValueError."""
    if not is_identifier(name):
        raise ValueError(f"unsafe {what} {name!r}: must match {IDENTIFIER_RE.pattern}")
    return name


def require_table_name(name, what="table"):
    if not is_table_name(name):
        raise ValueError(f"unsafe {what} {name!r}: must match {TABLE_RE.pattern}")
    return name


# ----------------------------------------------------------------- regex
_REPEATS = None


def _repeat_ops():
    global _REPEATS
    if _REPEATS is None:
        names = ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
        consts = getattr(_sre_parse, "_constants", None) or _sre_parse
        _REPEATS = {getattr(consts, n) for n in names if hasattr(consts, n)}
    return _REPEATS


def _children(av):
    """Yield sub-patterns nested inside one parsed op's argument."""
    if isinstance(av, _sre_parse.SubPattern):
        yield av
    elif isinstance(av, (list, tuple)):
        for x in av:
            yield from _children(x)


def _branch_op():
    consts = getattr(_sre_parse, "_constants", None) or _sre_parse
    return getattr(consts, "BRANCH", None)


def _has_branch(subpattern):
    for op, av in subpattern:
        if op == _branch_op():
            return True
        for child in _children(av):
            if _has_branch(child):
                return True
    return False


def _has_nested_unbounded(subpattern, inside_unbounded=False):
    for op, av in subpattern:
        if op in _repeat_ops():
            _lo, hi, body = av
            unbounded = hi == _MAXREPEAT or hi > _LARGE_REPEAT
            if unbounded and inside_unbounded:
                return True
            if unbounded and _has_branch(body):     # (a|aa)+ : overlapping alternatives, repeated
                return True
            if _has_nested_unbounded(body, inside_unbounded or unbounded):
                return True
        else:
            for child in _children(av):
                if _has_nested_unbounded(child, inside_unbounded):
                    return True
    return False


def regex_problem(pattern):
    """Return a human-readable reason the pattern is unsafe/invalid, or None if it's acceptable."""
    if not isinstance(pattern, str):
        return "regex must be a string"
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"regex is {len(pattern)} chars (max {MAX_PATTERN_LENGTH})"
    try:
        parsed = _sre_parse.parse(pattern)
    except re.error as e:
        return f"invalid regex: {e}"
    if _has_nested_unbounded(parsed):
        return ("regex nests unbounded quantifiers or repeats an alternation (e.g. (x+)+ or (a|aa)+) — "
                "catastrophic backtracking risk; use a character class or a bounded repeat instead")
    return None


@functools.lru_cache(maxsize=512)
def _compiled(pattern):
    return (_regex or re).compile(pattern)


def safe_search(pattern, text):
    """Search with the guards applied. Returns True/False, or None if the pattern is refused (too
    long / catastrophic shape / invalid) or the match exceeded REGEX_TIMEOUT_SECONDS — callers decide
    whether that fails open or closed."""
    if regex_problem(pattern):
        return None
    subject = str(text)[:MAX_REGEX_INPUT]
    if _regex is None:  # pragma: no cover - static checks only
        return re.search(pattern, subject) is not None
    try:
        return _compiled(pattern).search(subject, timeout=REGEX_TIMEOUT_SECONDS) is not None
    except TimeoutError:
        return None
