"""Opt-in request rate limiting for the runtime — per-client-key (IP), fixed window.

Off by default. Enable with `SURVEY_RATE_LIMIT="N/period"` (period ∈ s|sec|second|m|min|minute|h|hour),
e.g. `60/min` or `2/s`. Applied to the survey-serving paths (page loads + submit), never to /health.

In-process and **per-instance**: behind N replicas each enforces the limit independently — plenty for
abuse / burst / quota-burn / LLM-budget protection; use your load balancer or WAF for a strict global
cap. Pure stdlib, thread-safe. Memory is bounded by opportunistic pruning of expired windows.
"""
import threading
import time

_PERIODS = {"s": 1, "sec": 1, "second": 1, "seconds": 1,
            "m": 60, "min": 60, "minute": 60, "minutes": 60,
            "h": 3600, "hour": 3600, "hours": 3600}


def parse_rate(spec):
    """'60/min' -> (limit=60, window_seconds=60). None if empty/invalid (→ limiting off)."""
    if not spec:
        return None
    try:
        num, _, per = spec.strip().partition("/")
        limit = int(num.strip())
        window = _PERIODS[(per.strip() or "s").lower()]
        return (limit, window) if limit > 0 else None
    except Exception:
        return None


class RateLimiter:
    def __init__(self, limit, window, clock=None, max_keys=100_000):
        self.limit, self.window = limit, window
        self._clock = clock or time.monotonic          # injectable for tests
        self._lock = threading.Lock()
        self._buckets = {}                             # key -> [window_start, count]
        self._max_keys = max_keys

    def allow(self, key):
        """Return (allowed: bool, retry_after_seconds: int). Fixed window per key."""
        now = self._clock()
        with self._lock:
            start, count = self._buckets.get(key, (now, 0))
            if now - start >= self.window:             # window rolled over
                start, count = now, 0
            if count < self.limit:
                self._buckets[key] = (start, count + 1)
                if len(self._buckets) > self._max_keys:
                    self._buckets = {k: v for k, v in self._buckets.items()
                                     if now - v[0] < self.window}
                return True, 0
            self._buckets[key] = (start, count)
            return False, max(1, int(self.window - (now - start)))


def from_env(env=None):
    """Build a RateLimiter from SURVEY_RATE_LIMIT, or None when unset/invalid (limiting off)."""
    import os
    env = env if env is not None else os.environ
    parsed = parse_rate(env.get("SURVEY_RATE_LIMIT", ""))
    return RateLimiter(*parsed) if parsed else None
