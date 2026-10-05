"""End-to-end HTTP integration test: start the real runtime as a subprocess, drive respondents
through the actual HTTP form to a terminal, and assert the response persisted to the store and the
webhook passback fired (signed). This proves the full serve → submit → store → passback lifecycle
that unit tests (engine-level) don't cover.

Uses a subprocess (real process boundary) + an in-process webhook receiver. `python -m
survey_agent.cli` works both from an editable install (CI) and with PYTHONPATH=src (local), and the
subprocess inherits the parent env either way.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SURVEY = {"survey": {
    "id": "e2e-http", "version": 1, "title": "E2E HTTP screener",
    "settings": {"url_variables": ["uid", "rid"], "theme": "paper"},
    "pages": [
        {"id": "p1", "questions": [
            {"id": "q_consent", "type": "radio", "required": True, "title": "Consent?",
             "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]},
            {"id": "q_age", "type": "number", "required": True, "title": "Age?",
             "validation": {"min": 18, "max": 120}}]},
        {"id": "p2", "questions": [
            {"id": "q_channel", "type": "radio", "required": True, "title": "Channel?",
             "options": [{"value": "web", "label": "Web"}, {"value": "app", "label": "App"}]}]},
    ],
    "actions": [
        {"type": "disqualify", "reason_code": "no_consent", "dq_message": "Thanks anyway.",
         "when": {"ref": {"kind": "question", "id": "q_consent"}, "operator": "equals", "value": "no"}},
        {"type": "complete", "redirect": {"kind": "default"}},
    ],
}}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):  # a terminal 302 passback shouldn't be auto-followed
        return None


@pytest.fixture()
def live_server(tmp_path):
    """Serve the survey over real HTTP (sqlite store + signed webhook passbacks) with an in-process
    receiver capturing passbacks. Yields (base_url, db_path, captured)."""
    survey_path = tmp_path / "e2e-http.survey.json"
    survey_path.write_text(json.dumps(SURVEY))
    db = str(tmp_path / "e2e.db")
    runtime_port, recv_port = _free_port(), _free_port()

    captured = []

    class Recv(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
            captured.append({"event": payload.get("event"), "sig": self.headers.get("X-Survey-Signature")})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    recv = ThreadingHTTPServer(("127.0.0.1", recv_port), Recv)
    threading.Thread(target=recv.serve_forever, daemon=True).start()

    env = {**os.environ, "SURVEY_FILE": str(survey_path), "SURVEY_STORE": "sqlite",
           "SURVEY_SQLITE_PATH": db, "SURVEY_INTEGRATION": "webhook",
           "SURVEY_WEBHOOK_URL": f"http://127.0.0.1:{recv_port}/hook",
           "SURVEY_WEBHOOK_SECRET": "e2e-strong-secret", "SURVEY_HOST": "127.0.0.1",
           "PORT": str(runtime_port)}
    proc = subprocess.Popen([sys.executable, "-m", "survey_agent.cli", "serve"], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{runtime_port}"
    try:
        for _ in range(150):
            if proc.poll() is not None:
                pytest.fail("runtime exited early:\n" + (proc.stdout.read() if proc.stdout else ""))
            try:
                if urllib.request.urlopen(base + "/health", timeout=1).status == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.fail("runtime /health never came up")
        yield base, db, captured
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        recv.shutdown()


def _submit(base, sid, page_id, rid, uid, answers):
    op = urllib.request.build_opener(_NoRedirect)
    data = urllib.parse.urlencode({"session_id": f"{sid}:{rid}:{uid}", "page_id": page_id, **answers}).encode()
    req = urllib.request.Request(f"{base}/s/{sid}/submit", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with op.open(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_http_lifecycle_complete_and_screenout(live_server):
    from survey_agent.store.sqlite import SQLiteStore
    base, db, captured = live_server
    sid = "e2e-http"

    # Respondent 1 — qualifies → completes (GET creates the session, then walk the pages).
    assert urllib.request.urlopen(f"{base}/s/{sid}?uid=U1&rid=R1", timeout=5).status == 200
    assert _submit(base, sid, "p1", "R1", "U1", {"q_consent": "yes", "q_age": "30"}) in (200, 302)
    assert _submit(base, sid, "p2", "R1", "U1", {"q_channel": "web"}) in (200, 302)

    # Respondent 2 — declines consent → screened out.
    urllib.request.urlopen(f"{base}/s/{sid}?uid=U2&rid=R2", timeout=5)
    assert _submit(base, sid, "p1", "R2", "U2", {"q_consent": "no", "q_age": "40"}) in (200, 302)

    # --- persistence (poll briefly; the write lands just before the passback) ---
    store = SQLiteStore(db)
    rows = []
    for _ in range(20):
        rows = store.list_responses(sid)
        if len(rows) >= 2:
            break
        time.sleep(0.1)
    statuses = sorted(r.get("status") for r in rows)
    assert statuses == ["Complete", "Disqualified"], statuses
    u1 = store.list_responses(sid, uid="U1")[0]
    answered = u1.get("survey_data") or u1.get("answers") or {}
    assert {"q_consent", "q_age", "q_channel"} <= set(answered), list(answered)

    # --- passbacks: started+completed for R1, started+terminated for R2, all signed ---
    for _ in range(20):
        if len(captured) >= 4:
            break
        time.sleep(0.1)
    events = [c["event"] for c in captured]
    assert "completed" in events, events
    assert any(e in ("terminated", "screened_out", "disqualified") for e in events), events
    assert all(c["sig"] for c in captured), "passbacks must be HMAC-signed when a secret is set"
