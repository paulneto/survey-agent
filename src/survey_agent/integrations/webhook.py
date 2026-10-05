"""webhook — POST the neutral event payload to one configured URL on start + terminal, and deliver
mid-survey `webhook` action intents. Generic: works with Zapier / n8n / your own API / a serverless
capture endpoint. At-least-once delivery; the payload carries an `idempotency_key` so the receiver
can dedupe (prod: back with a durable outbox).

**Signing.** If a shared secret is configured (`SURVEY_WEBHOOK_SECRET`), every POST is signed with
an HMAC-SHA256 over `"{timestamp}.{body}"` (a Stripe-style, replay-resistant scheme):

    X-Survey-Signature: t=<unix_ts>,v1=<hex_hmac>

Receivers recompute it with `verify(secret, body, header)` and reject stale/forged calls. The
secret is never sent — only the signature."""
import hashlib
import hmac
import json
import os
import time
import urllib.request

from survey_agent.engine import terminal
from .base import Integration


def _secret(explicit=None):
    return explicit if explicit is not None else os.environ.get("SURVEY_WEBHOOK_SECRET", "")


def sign(secret, body, ts=None):
    """Return (timestamp, header_value) for an HMAC-SHA256 signature over '{ts}.{body}'."""
    body = body if isinstance(body, bytes) else str(body).encode()
    ts = str(int(ts if ts is not None else time.time()))
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return ts, f"t={ts},v1={mac}"


def verify(secret, body, header, tolerance=300):
    """Verify a signature header against the raw body. Returns True iff the HMAC matches and the
    timestamp is within `tolerance` seconds (replay protection). For receivers."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        ts, v1 = parts.get("t", ""), parts.get("v1", "")
        if tolerance and abs(int(time.time()) - int(ts)) > tolerance:
            return False
        expected = sign(secret, body, ts=ts)[1].split("v1=")[1]
        return hmac.compare_digest(expected, v1)
    except Exception:
        return False


def deliver(intent, timeout=5, secret=None):
    """Deliver one `webhook` action intent ({method, url, payload}). Best-effort, fail-open; in
    production back this with a durable outbox + retries. Signs POSTs when a secret is set.
    Returns True on apparent success."""
    url = intent.get("url")
    if not url:
        return False
    method = (intent.get("method") or "POST").upper()
    payload = intent.get("payload") or {}
    sec = _secret(secret)
    try:
        if method == "GET":
            from urllib.parse import urlencode
            sep = "&" if "?" in url else "?"
            req = urllib.request.Request(url + (sep + urlencode(payload) if payload else ""), method="GET")
        else:
            body = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
            if sec:
                ts, headers["X-Survey-Signature"] = sign(sec, body)
                headers["X-Survey-Timestamp"] = ts
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception:
        return False  # prod: enqueue for retry


class WebhookIntegration(Integration):
    def __init__(self, url=None, secret=None):
        self.url = url or os.environ.get("SURVEY_WEBHOOK_URL")
        self.secret = secret or os.environ.get("SURVEY_WEBHOOK_SECRET", "")

    def started(self, session):
        self._post(terminal.build_event(session, "started"))

    def terminal(self, session, status, reason_code=None):
        self._post(terminal.build_event(session, status, reason_code=reason_code))

    def _post(self, event):
        if not self.url:
            return
        body = json.dumps(event).encode()
        headers = {"Content-Type": "application/json", "X-Survey-Event": event["event"]}
        if self.secret:
            ts, headers["X-Survey-Signature"] = sign(self.secret, body)
            headers["X-Survey-Timestamp"] = ts
        req = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass  # prod: leave in a durable outbox for retry
