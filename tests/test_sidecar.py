import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from survey_agent.sidecar.runner import TriggerRunner, StaticDetector, survey_url_for, load_triggers
from survey_agent.sidecar.panel_client import NullPanelClient, PanelClient, build_launch_request
from survey_agent.integrations.webhook import verify

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
TRIGGER = {
    "id": "streaming-search-trigger",
    "survey": "streaming-usage",
    "detect": {
        "event_type": "web_search", "window": {"last": "1d"},
        "criteria": {"op": "or", "conditions": [
            {"field": "search_term", "operator": "contains_any", "value": ["netflix", "disney", "what to watch"]}]},
        "operation": "exists",
    },
    "launch": {"target_completes": 500, "budget": {"currency": "USD", "max_total": 100}},
    "throttle": {"cooldown_days": 30, "max_offers_per_run": 500},
}


def events():
    t = NOW.isoformat()
    return [
        {"respondent_id": "u-1001", "search_term": "netflix price", "event_time": t},
        {"respondent_id": "u-1002", "search_term": "best pizza", "event_time": t},      # no match
        {"respondent_id": "u-1003", "search_term": "disney reviews", "event_time": t},
        {"respondent_id": "u-1001", "search_term": "what to watch this weekend", "event_time": t},   # dup uid
    ]


def test_detects_and_launches_to_matched_respondents():
    runner = TriggerRunner(StaticDetector(events(), NOW), NullPanelClient(quiet=True))
    res = runner.run_once(TRIGGER, now_ts=NOW.timestamp())
    assert res["matched"] == 2          # u-1001 and u-1003 (u-1002 excluded, u-1001 de-duped)
    assert res["offered"] == 2
    assert res["launch_id"] == "launch-1"
    req = runner.panel.calls[0]
    assert set(req["respondent_ids"]) == {"u-1001", "u-1003"}
    assert req["type"] == "survey.launch_request" and req["version"] == 1
    assert req["survey_id"] == "streaming-usage"
    assert req["target_completes"] == 500 and req["budget"]["max_total"] == 100
    assert req["audience"] == TRIGGER["detect"]


def test_cooldown_suppresses_repeat_offers():
    runner = TriggerRunner(StaticDetector(events(), NOW), NullPanelClient(quiet=True))
    runner.run_once(TRIGGER, now_ts=NOW.timestamp())
    res2 = runner.run_once(TRIGGER, now_ts=NOW.timestamp())
    assert res2["offered"] == 0         # within 30-day cooldown


def test_failed_launch_does_not_start_cooldown_or_advance_watermark():
    class Boom:
        def launch(self, request):
            raise OSError("panel down")
    runner = TriggerRunner(StaticDetector(events(), NOW), Boom())
    with pytest.raises(OSError):
        runner.run_once(TRIGGER, now_ts=NOW.timestamp())
    assert runner.state["cooldown"] == {} and runner.state["watermark"] == {}


def test_launch_request_idempotency_key_is_order_independent():
    a = build_launch_request(TRIGGER, "u", ["u-2", "u-1"])
    b = build_launch_request(TRIGGER, "u", ["u-1", "u-2"])
    assert a["idempotency_key"] == b["idempotency_key"]
    assert "budget" in a and "metadata" not in a


def test_survey_url_has_placeholders():
    url = survey_url_for("streaming-usage")
    assert "/s/streaming-usage?" in url and "uid={uid}" in url and "rid={rid}" in url


def test_load_triggers_skips_non_trigger_json(tmp_path):
    (tmp_path / "t.json").write_text(json.dumps(TRIGGER))
    (tmp_path / "x.survey.json").write_text(json.dumps({"survey": {"id": "x"}}))
    (tmp_path / "other.json").write_text(json.dumps({"hello": 1}))
    assert [t["id"] for t in load_triggers(str(tmp_path))] == ["streaming-search-trigger"]


def test_panel_client_posts_signed_launch_request():
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.update(body=body, auth=self.headers.get("Authorization"),
                       sig=self.headers.get("X-Survey-Signature"))
            out = b'{"launch_id": "L-1"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        client = PanelClient(url=f"http://127.0.0.1:{srv.server_port}/launch", token="tok", secret="shh")
        req = build_launch_request(TRIGGER, survey_url_for("streaming-usage"), ["u-1001"])
        assert client.launch(req) == {"launch_id": "L-1"}
    finally:
        srv.server_close()
    assert got["auth"] == "Bearer tok"
    assert verify("shh", got["body"], got["sig"])
    assert json.loads(got["body"])["respondent_ids"] == ["u-1001"]


def test_panel_client_requires_url():
    with pytest.raises(RuntimeError):
        PanelClient(url="", token="", secret="").launch({})
