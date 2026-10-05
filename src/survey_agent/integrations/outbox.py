"""Durable outbox for webhook-action delivery — retry + dead-letter, so a dropped passback isn't lost.

Off by default (delivery stays best-effort, fire-once). Enable with `SURVEY_OUTBOX=1`; then a
webhook-action intent that fails to deliver is written to a durable queue and retried with
exponential backoff until it succeeds or exhausts `SURVEY_OUTBOX_MAX_ATTEMPTS` (default 8), at which
point it moves to a dead-letter folder for inspection.

File-based (one JSON file per intent) to match the "everything is files" ethos — inspectable,
S3-syncable, no server. Directory: `SURVEY_OUTBOX_DIR` (default `<SURVEY_DATA_DIR>/outbox`). It's
per-instance on local disk; point it at a shared/persistent volume (or implement a DB-backed outbox
against this same interface) for a multi-host deployment.

Retry is driven either by `survey-agent outbox retry` (a cron / manual sweep) or, when the runtime
runs with `SURVEY_OUTBOX_RETRY_SECONDS=N`, a background thread that sweeps every N seconds.
"""
import hashlib
import json
import os
import time


def _intent_id(intent):
    """Stable id for an intent: its idempotency_key if present, else a content hash. Webhook actions
    fire once per session, so the content hash uniquely (and idempotently) identifies a delivery."""
    key = (intent.get("payload") or {}).get("idempotency_key")
    if key:
        return "k_" + hashlib.sha1(str(key).encode()).hexdigest()[:16]
    return "h_" + hashlib.sha1(json.dumps(intent, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Outbox:
    def __init__(self, dir=None, max_attempts=8, backoff_cap=3600):
        self.dir = os.path.abspath(
            dir or os.environ.get("SURVEY_OUTBOX_DIR")
            or os.path.join(os.environ.get("SURVEY_DATA_DIR", "./survey_data"), "outbox"))
        self.dead_dir = os.path.join(self.dir, "dead")
        os.makedirs(self.dead_dir, exist_ok=True)
        self.max_attempts = max_attempts
        self.backoff_cap = backoff_cap

    def _path(self, item_id):
        return os.path.join(self.dir, f"{item_id}.json")

    def _write(self, path, rec):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, path)

    def _next_try(self, attempts, now=None):
        now = now if now is not None else time.time()
        return now + min(2 ** attempts, self.backoff_cap)

    def deliver_or_enqueue(self, intent, deliver_fn):
        """Try to deliver now; on failure durably enqueue for retry. Returns True iff delivered."""
        if deliver_fn(intent):
            return True
        self.enqueue(intent)
        return False

    def enqueue(self, intent, attempts=1, last_error="delivery failed"):
        item_id = _intent_id(intent)
        self._write(self._path(item_id), {
            "id": item_id, "intent": intent, "attempts": attempts,
            "last_error": str(last_error)[:300], "next_try": self._next_try(attempts)})
        return item_id

    def _items(self):
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".json") and not name.endswith(".tmp"):
                path = os.path.join(self.dir, name)
                try:
                    with open(path) as f:
                        yield path, json.load(f)
                except Exception:
                    continue

    def retry(self, deliver_fn, now=None):
        """Attempt every due pending item once. Delivered → removed; still failing → backoff bumped;
        past max_attempts → moved to dead/. Returns {attempted, delivered, dead, pending}."""
        now = now if now is not None else time.time()
        attempted = delivered = dead = 0
        for path, rec in self._items():
            if rec.get("next_try", 0) > now:
                continue
            attempted += 1
            if deliver_fn(rec["intent"]):
                os.remove(path)
                delivered += 1
                continue
            rec["attempts"] = rec.get("attempts", 1) + 1
            rec["last_error"] = "delivery failed"
            if rec["attempts"] > self.max_attempts:
                self._write(os.path.join(self.dead_dir, os.path.basename(path)), rec)
                os.remove(path)
                dead += 1
            else:
                rec["next_try"] = self._next_try(rec["attempts"], now)
                self._write(path, rec)
        return {"attempted": attempted, "delivered": delivered, "dead": dead,
                "pending": self.stats()["pending"]}

    def stats(self):
        pending = sum(1 for n in os.listdir(self.dir) if n.endswith(".json") and not n.endswith(".tmp"))
        dead = sum(1 for n in os.listdir(self.dead_dir) if n.endswith(".json"))
        return {"pending": pending, "dead": dead}

    def list(self, dead=False):
        d = self.dead_dir if dead else self.dir
        out = []
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name)) as f:
                    rec = json.load(f)
                out.append({"id": rec.get("id"), "attempts": rec.get("attempts"),
                            "last_error": rec.get("last_error"),
                            "url": (rec.get("intent") or {}).get("url")})
            except Exception:
                continue
        return out


def enabled(env=None):
    env = env if env is not None else os.environ
    return (env.get("SURVEY_OUTBOX") or "").lower() in ("1", "true", "yes")


def from_env(env=None):
    """Return an Outbox when SURVEY_OUTBOX is truthy, else None (delivery stays best-effort)."""
    env = env if env is not None else os.environ
    if not enabled(env):
        return None
    return Outbox(max_attempts=int(env.get("SURVEY_OUTBOX_MAX_ATTEMPTS", "8")))
