"""Structured logging for the runtime + gateway — the operator's error/observability stream.

Off-path from the pure engine (the engine never imports this). `configure()` sets up the
`survey_agent` logger from env; `get_logger(name)` returns a namespaced child.

  SURVEY_LOG_LEVEL   default INFO     — DEBUG | INFO | WARNING | ERROR
  SURVEY_LOG_FORMAT  default text     — text | json  (json = one object per line, for log pipelines)

**Never log PII** — answers, free-text, or url_variables. Log identifiers only (survey_id, event,
a redacted url, a hashed session id). Attach them via the standard `extra=` kwarg; the JSON formatter
emits them as top-level fields, the text formatter appends them.

Defence in depth, applied by both formatters to `extra=` fields:
  * `session_id` / `response_id` → a short stable hash (`redact_id`). A respondent session id is
    `survey:<rid>:<uid>`, i.e. it *encodes* the invite + respondent ids, so it is never logged raw.
  * `url` → `scheme://host[:port]#<fingerprint>` (`redact_url`): the origin plus the first 8 hex of
    sha256(full url) — never the path, query, fragment or userinfo. Webhook endpoints often carry their
    credential as a *path* segment (`/hooks/<token>`), so the path is treated as secret too; the
    fingerprint still lets an operator correlate log lines with a configured endpoint
    (`obs.url_fingerprint(url)`).
"""
import hashlib
import json
import logging
import os
import sys
import re
from urllib.parse import urlsplit

_CONFIGURED = False
_STD = set(logging.makeLogRecord({}).__dict__) | {"taskName"}  # standard LogRecord attrs to skip


_ID_FIELDS = ("session_id", "response_id")
_URL_FIELDS = ("url",)


def redact_id(value):
    """A short, stable, non-reversible handle for an identifier (correlate log lines without
    exposing the respondent/invite ids a session id encodes). None stays None."""
    if value is None or value == "":
        return value
    return "h:" + hashlib.sha256(str(value).encode()).hexdigest()[:12]


def url_fingerprint(url):
    """A short, stable, non-reversible handle for a full URL (first 8 hex of its sha256), so an
    operator can tell which configured endpoint a log line is about without the line exposing it."""
    return hashlib.sha256(str(url).encode()).hexdigest()[:8]


_REDACTED_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://[^/?#@\s]+|<redacted>)#[0-9a-f]{8}$")


def redact_url(url):
    """`scheme://host[:port]#<fingerprint>` for `url` — the origin plus `url_fingerprint(url)`. The
    path, query string, fragment and userinfo are all dropped: any of them can carry a credential
    (webhook endpoints commonly embed a token as a path segment), a session token, or a respondent id.
    Unparseable input becomes "<redacted>" (still fingerprinted when non-empty)."""
    if not url:
        return url
    if _REDACTED_RE.match(str(url)):          # already redacted (call site + formatter): keep it stable
        return str(url)
    fp = url_fingerprint(url)
    try:
        p = urlsplit(str(url))
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        if not (p.scheme and host):
            return f"<redacted>#{fp}"
        return f"{p.scheme}://{host}#{fp}"
    except Exception:
        return f"<redacted>#{fp}"


def _extras(record):
    out = {}
    for k, v in record.__dict__.items():
        if k in _STD or k.startswith("_"):
            continue
        if k in _ID_FIELDS:
            v = redact_id(v)
        elif k in _URL_FIELDS or k.endswith("_url"):
            v = redact_url(v)
        out[k] = v
    return out


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        d = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
             "logger": record.name, "msg": record.getMessage()}
        d.update(_extras(record))
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record):
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname} {record.name}: {record.getMessage()}"
        ex = _extras(record)
        if ex:
            base += "  " + " ".join(f"{k}={v}" for k, v in ex.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure(force=False, env=None):
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    env = env if env is not None else os.environ
    level = (env.get("SURVEY_LOG_LEVEL") or "INFO").upper()
    fmt = (env.get("SURVEY_LOG_FORMAT") or "text").lower()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _TextFormatter())
    root = logging.getLogger("survey_agent")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    _CONFIGURED = True


def get_logger(name="survey_agent"):
    configure()
    return logging.getLogger(name)
