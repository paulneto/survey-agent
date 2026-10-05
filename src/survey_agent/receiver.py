"""Mock webhook receiver for the local demo.

Stands in for whatever you wire the `webhook` integration to (a panel, a CRM, your own API). It
accepts the neutral terminal-event JSON the engine posts on start/complete/screen-out, dedupes on
idempotency_key, keeps an in-memory ledger keyed by the URL variables (uid/rid), and prints each
event so you can watch the flow live.

Run standalone:  python -m survey_agent.receiver           # listens on :9000, endpoint POST /hook
Or `survey-agent demo` starts it alongside the runtime for you.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
     "r": "\033[31m", "c": "\033[36m", "x": "\033[0m"}


class ReceiverState:
    def __init__(self):
        self.lock = threading.Lock()
        self.ledger = {}        # (uid, rid) -> {"status", "reason", "history": [...]}
        self.seen = set()       # idempotency_key dedupe

    def record(self, event):
        key = event.get("idempotency_key")
        with self.lock:
            deduped = key in self.seen
            if not deduped and key:
                self.seen.add(key)
            uv = event.get("url_variables", {}) or {}
            uid, rid = uv.get("uid"), uv.get("rid")
            entry = self.ledger.setdefault((uid, rid), {"status": None, "reason": None, "history": []})
            if not deduped:
                entry["status"] = event.get("status")
                entry["reason"] = event.get("reason_code")
                entry["history"].append(event.get("event"))
            return deduped


STATE = ReceiverState()


def _print(event, deduped):
    status = (event.get("status") or event.get("event") or "").upper()
    color = {"COMPLETED": C["g"], "DISQUALIFIED": C["y"], "OVERQUOTA": C["y"],
             "USER_TERMINATED": C["r"], "STARTED": C["c"]}.get(status, C["c"])
    uv = event.get("url_variables", {}) or {}
    reason = f"  reason={event.get('reason_code')}" if event.get("reason_code") else ""
    tag = f"{C['dim']}(dup, ignored){C['x']}" if deduped else ""
    print(f"{C['b']}[RECEIVER]{C['x']} uid={uv.get('uid')} rid={uv.get('rid')} "
          f"{color}{status}{C['x']}  event={event.get('event')}{reason} {tag}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/ledger"):
            with STATE.lock:
                rows = [{"uid": k[0], "rid": k[1], **v} for k, v in STATE.ledger.items()]
            return self._json(200, {"ledger": rows})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/hook":
            return self._json(404, {"error": f"unknown endpoint {self.path}"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            event = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        deduped = STATE.record(event)
        _print(event, deduped)
        self._json(200, {"ok": True, "deduped": deduped})


def serve(port=9000):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"{C['b']}[RECEIVER]{C['x']} mock webhook receiver on http://127.0.0.1:{port}/hook  "
          f"(watch this terminal for survey events)")
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
