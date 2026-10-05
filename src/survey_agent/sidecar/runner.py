"""Behavioral trigger sidecar — watch a behavioral warehouse for a behavior and launch a survey to
the matched respondents through your panel.

Flow per trigger (poll loop, incremental on an event-time watermark):
  1. detect distinct respondent ids whose behavior matches `detect` (event_type+window+criteria)
     since the last watermark  (FireboltDetector compiles one query via enrich.firebolt_sql)
  2. drop anyone in cooldown / over the throttle caps
  3. send ONE generic launch request (see sidecar/panel_client.py for the contract) carrying the
     pre-approved survey's survey_url (placeholders {uid}/{rid}) and the matched cohort
  4. the panel sends each person the link → they open the survey_url → the survey pulls their
     actual behavior via the enrichment layer and asks "why?" → lifecycle events flow back through
     the configured integration as normal
  5. record cooldown; advance the watermark

Survey creation: it references a PRE-APPROVED survey (governance). Personalisation per respondent
("why did you search for *your* term?") is done at runtime via enrichment, NOT by generating a new
survey per event. Net-new trigger surveys go through the human-gated author-survey → test-survey →
launch-survey loop first.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey_agent.enrich import spec as spec_mod  # noqa: E402
from survey_agent.sidecar.panel_client import PanelClient, NullPanelClient, build_launch_request  # noqa: E402

PUBLIC_BASE = os.environ.get("SURVEY_PUBLIC_BASE_URL", "https://survey.example.com")
TRIGGERS_DIR = os.environ.get("SURVEY_TRIGGERS_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def load_triggers(directory=TRIGGERS_DIR):
    out = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json") and not name.startswith("_") and not name.endswith(".survey.json"):
            with open(os.path.join(directory, name)) as f:
                t = json.load(f)
            # only trigger files (id + detect + survey); other example JSON in the dir is skipped
            if isinstance(t, dict) and {"id", "detect", "survey"} <= t.keys() and t.get("enabled", True):
                out.append(t)
    return out


def survey_url_for(survey_id):
    return f"{PUBLIC_BASE}/s/{survey_id}?uid={{uid}}&rid={{rid}}&group=trigger"


# ----------------------------------------------------------------- detectors
class FireboltDetector:
    """Production detector: one compiled DISTINCT-respondent query per trigger, incremental."""
    def __init__(self, connect_kwargs=None):
        self.connect_kwargs = connect_kwargs or {}

    def find_matches(self, detect_spec, since=None, limit=10000):
        from survey_agent.enrich.firebolt_sql import compile_detect_sql
        sql, params = compile_detect_sql(detect_spec, since=since, limit=limit)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [(str(row[0]), str(row[1])) for row in cur.fetchall()]  # (respondent_id, last_event)
        finally:
            conn.close()

    def _connect(self):  # pragma: no cover - needs firebolt-sdk + creds
        from firebolt.db import connect
        from firebolt.client.auth import ClientCredentials
        return connect(auth=ClientCredentials(**self.connect_kwargs.get("auth", {})),
                       **{k: v for k, v in self.connect_kwargs.items() if k != "auth"})


class StaticDetector:
    """Demo/test detector: matches over an in-memory event list (each event has the respondent id
    column — `respondent_id` by default, see enrich.spec.ID_FIELD — and event_time). Applies the
    same window+criteria as production."""
    def __init__(self, events, now, id_field=None):
        self.events = events
        self.now = now
        self.id_field = id_field or spec_mod.ID_FIELD

    def find_matches(self, detect_spec, since=None, limit=10000):
        lo = spec_mod.window_start(detect_spec.get("window"), self.now)
        matched = {}
        for e in self.events:
            t = e.get("event_time")
            if lo is not None and t is not None and t < (lo.isoformat() if hasattr(lo, "isoformat") else lo):
                continue
            if since is not None and t is not None and t <= since:
                continue
            if spec_mod._match(detect_spec.get("criteria"), e):
                uid = str(e.get(self.id_field))
                matched[uid] = max(matched.get(uid, ""), str(t or ""))
        return list(matched.items())[:limit]


# ----------------------------------------------------------------- runner
class TriggerRunner:
    def __init__(self, detector, panel=None, state=None):
        self.detector = detector
        self.panel = panel or NullPanelClient()
        self.state = state or {"watermark": {}, "cooldown": {}}  # cooldown: {(trigger,uid): ts}

    def _eligible(self, trigger, uid, now_ts):
        cd = trigger.get("throttle", {}).get("cooldown_days", 0)
        last = self.state["cooldown"].get(f"{trigger['id']}:{uid}")
        return not (cd and last and (now_ts - last) < cd * 86400)

    def run_once(self, trigger, now_ts=0):
        since = self.state["watermark"].get(trigger["id"])
        matches = self.detector.find_matches(trigger["detect"], since=since)
        if not matches:
            return {"trigger": trigger["id"], "matched": 0, "offered": 0}
        cap = trigger.get("throttle", {}).get("max_offers_per_run", 500)
        fresh = [(uid, t) for uid, t in matches if self._eligible(trigger, uid, now_ts)][:cap]
        watermark = max((t for _, t in matches), default=since)
        launch_id = None
        if fresh:
            ids = [uid for uid, _ in fresh]
            request = build_launch_request(trigger, survey_url_for(trigger["survey"]), ids)
            resp = self.panel.launch(request) or {}  # raises on failure -> no cooldown, retried next poll
            launch_id = resp.get("launch_id")
            for uid in ids:
                self.state["cooldown"][f"{trigger['id']}:{uid}"] = now_ts
        self.state["watermark"][trigger["id"]] = watermark
        return {"trigger": trigger["id"], "matched": len(matches), "offered": len(fresh),
                "launch_id": launch_id}

    def run_forever(self, triggers, now_fn=time.time, sleep_fn=time.sleep):  # pragma: no cover
        while True:
            for t in triggers:
                res = self.run_once(t, now_ts=now_fn())
                print(f"[sidecar] {res}")
            sleep_fn(min(t.get("poll_interval_seconds", 60) for t in triggers))


if __name__ == "__main__":  # pragma: no cover
    runner = TriggerRunner(FireboltDetector(), PanelClient())
    runner.run_forever(load_triggers())
