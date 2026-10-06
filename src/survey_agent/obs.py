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
  * `url` → scheme://host/path only (`redact_url`): no query string (tokens, ids) and no userinfo.
"""
import hashlib
import json
import logging
import os
import sys
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


def redact_url(url):
    """scheme://host[:port]/path of `url` — drops userinfo, query string and fragment, which can
    carry credentials, tokens, or respondent ids. Unparseable input becomes "<redacted>"."""
    if not url:
        return url
    try:
        p = urlsplit(str(url))
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        if not (p.scheme and host):
            return "<redacted>"
        return f"{p.scheme}://{host}{p.path}"
    except Exception:
        return "<redacted>"


def _extras(record):
    out = {}
    for k, v in record.__dict__.items():
        if k in _STD or k.startswith("_"):
            continue
        if k in _ID_FIELDS:
            v = redact_id(v)
        elif k in _URL_FIELDS:
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
