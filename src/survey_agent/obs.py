"""Structured logging for the runtime + gateway — the operator's error/observability stream.

Off-path from the pure engine (the engine never imports this). `configure()` sets up the
`survey_agent` logger from env; `get_logger(name)` returns a namespaced child.

  SURVEY_LOG_LEVEL   default INFO     — DEBUG | INFO | WARNING | ERROR
  SURVEY_LOG_FORMAT  default text     — text | json  (json = one object per line, for log pipelines)

**Never log PII** — answers, free-text, or url_variables. Log identifiers only (survey_id,
session_id, event, url). Attach them via the standard `extra=` kwarg; the JSON formatter emits them
as top-level fields, the text formatter appends them.
"""
import json
import logging
import os
import sys

_CONFIGURED = False
_STD = set(logging.makeLogRecord({}).__dict__) | {"taskName"}  # standard LogRecord attrs to skip


def _extras(record):
    return {k: v for k, v in record.__dict__.items() if k not in _STD and not k.startswith("_")}


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
