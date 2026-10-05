"""Panel launch client — how the trigger sidecar hands a matched cohort to *your* panel.

The sidecar never talks a vendor-specific API. When a trigger matches respondents it sends ONE
generic **launch request** to a URL you configure, and your panel (or a thin adapter in front of
it) decides how to offer the survey, enforce eligibility/fraud/quotas, and pay incentives. Results
come back through the normal integration seam (`SURVEY_INTEGRATION`): the neutral lifecycle events
`started`, `completed`, `terminated`, `terminated_by_user` (see `engine/terminal.py`).

Launch-request contract (v1)
----------------------------
    POST $PANEL_LAUNCH_URL
    Content-Type: application/json
    Authorization: Bearer $PANEL_LAUNCH_TOKEN          (only if set)
    X-Survey-Signature: t=<ts>,v1=<hmac>               (only if PANEL_LAUNCH_SECRET is set;
                                                        same scheme as integrations.webhook.sign)

    {
      "type": "survey.launch_request",
      "version": 1,
      "idempotency_key": "<trigger_id>:<sha256 of the sorted respondent ids>",
      "trigger_id": "streaming-search-trigger",
      "survey_id": "streaming-usage",
      "survey_url": "https://survey.example.com/s/streaming-usage?uid={uid}&rid={rid}&group=trigger",
      "respondent_ids": ["u-1001", "u-1003"],      # the matched cohort (your panel's ids)
      "audience": { ... },                         # the trigger's `detect` spec, for the record
      "target_completes": 2000,                    # optional (trigger.launch.target_completes)
      "budget": { ... },                           # optional, opaque (trigger.launch.budget)
      "metadata": { ... }                          # optional, opaque (trigger.launch.metadata)
    }

The panel substitutes its own respondent id for `{uid}` and its own response/assignment id for
`{rid}` when it sends each person the link. Response: any JSON; if it carries `launch_id` the
sidecar records it. A non-2xx response raises, and the sidecar does not start a cooldown for that
cohort, so the next poll retries (the idempotency key lets the panel dedupe the retry).

`NullPanelClient` records (and prints) the request instead of sending it — used by the demo/tests.
"""
import hashlib
import json
import os
import urllib.request

LAUNCH_TYPE = "survey.launch_request"
LAUNCH_VERSION = 1


def build_launch_request(trigger, survey_url, respondent_ids):
    """Build the v1 launch-request payload for a trigger's matched cohort."""
    ids = [str(i) for i in respondent_ids]
    launch = trigger.get("launch") or {}
    digest = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:16]
    body = {
        "type": LAUNCH_TYPE,
        "version": LAUNCH_VERSION,
        "idempotency_key": f"{trigger.get('id')}:{digest}",
        "trigger_id": trigger.get("id"),
        "survey_id": trigger.get("survey"),
        "survey_url": survey_url,
        "respondent_ids": ids,
        "audience": trigger.get("detect"),
    }
    for k in ("target_completes", "budget", "metadata"):
        if launch.get(k) is not None:
            body[k] = launch[k]
    return body


class PanelClient:
    """POSTs launch requests to PANEL_LAUNCH_URL (optionally bearer-authed and HMAC-signed)."""

    def __init__(self, url=None, token=None, secret=None, timeout=10):
        self.url = url or os.environ.get("PANEL_LAUNCH_URL", "")
        self.token = token if token is not None else os.environ.get("PANEL_LAUNCH_TOKEN", "")
        self.secret = secret if secret is not None else os.environ.get("PANEL_LAUNCH_SECRET", "")
        self.timeout = timeout

    def launch(self, request):
        if not self.url:
            raise RuntimeError("PANEL_LAUNCH_URL is not set — nowhere to send the launch request")
        body = json.dumps(request).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.secret:
            from survey_agent.integrations.webhook import sign
            ts, headers["X-Survey-Signature"] = sign(self.secret, body)
            headers["X-Survey-Timestamp"] = ts
        req = urllib.request.Request(self.url, method="POST", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}


class NullPanelClient:
    """Demo/dry-run: records what would have been sent."""

    def __init__(self, quiet=False):
        self.calls = []
        self.quiet = quiet

    def launch(self, request):
        launch_id = f"launch-{len(self.calls) + 1}"
        self.calls.append(request)
        if not self.quiet:
            ids = request["respondent_ids"]
            print(f"  [panel] would launch survey '{request['survey_id']}' to {len(ids)} "
                  f"respondent(s): {ids[:10]} → {request['survey_url']}")
        return {"launch_id": launch_id}
