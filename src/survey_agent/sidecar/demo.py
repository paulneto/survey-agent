"""Local demo of the behavioral trigger sidecar (no warehouse, no real panel).

    python -m survey_agent.sidecar.demo

Feeds a few recent searches through the streaming trigger and shows the launch request it would
send to your panel for the matched respondents. Re-running shows cooldown suppressing repeats.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey_agent.sidecar.runner import TriggerRunner, StaticDetector, load_triggers
from survey_agent.sidecar.panel_client import NullPanelClient


def main():
    now = datetime.now(timezone.utc)
    t_iso = now.isoformat()
    events = [
        {"respondent_id": "u-1001", "search_term": "netflix price uk", "event_time": t_iso},
        {"respondent_id": "u-1002", "search_term": "best pizza near me", "event_time": t_iso},
        {"respondent_id": "u-1003", "search_term": "disney reviews", "event_time": t_iso},
        {"respondent_id": "u-1001", "search_term": "what to watch this weekend", "event_time": t_iso},
        {"respondent_id": "u-1004", "search_term": "streaming cost", "event_time": t_iso},
    ]
    runner = TriggerRunner(StaticDetector(events, now), NullPanelClient())
    triggers = load_triggers()

    print("━━━ trigger sidecar demo ━━━")
    print(f"watching {len(triggers)} trigger(s); {len(events)} recent search events\n")
    for trig in triggers:
        print(f"trigger '{trig['id']}' → survey '{trig['survey']}'")
        res = runner.run_once(trig, now_ts=now.timestamp())
        print(f"  result: matched {res['matched']} respondent(s), offered {res['offered']}\n")

    print("second run (same events, cooldown active):")
    for trig in triggers:
        res = runner.run_once(trig, now_ts=now.timestamp())
        print(f"  '{trig['id']}': matched {res['matched']}, offered {res['offered']}  "
              f"(cooldown suppressed re-offers)")


if __name__ == "__main__":
    main()
