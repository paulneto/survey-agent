from survey_agent.engine.runtime import SurveyRuntime
from survey_agent.integrations.webhook import deliver

SURVEY = {"survey": {
    "id": "wh", "title": "WH", "settings": {"url_variables": ["uid"]},
    "pages": [
        {"id": "p1", "questions": [{"id": "q1", "type": "radio", "required": True,
                                    "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]},
        {"id": "p2", "questions": [{"id": "q2", "type": "text"}]},
    ],
    "actions": [
        {"type": "webhook",
         "when": {"ref": {"kind": "question", "id": "q1"}, "operator": "equals", "value": "yes"},
         "url": "https://hook.example/x?uid={{url:uid}}",
         "payload": {"answer": "{{q:q1.answer}}", "uid": "{{url:uid}}"}},
        {"type": "complete", "redirect": {"kind": "default"}},
    ],
}}


def _rt():
    return SurveyRuntime(SURVEY)


def test_webhook_action_emits_resolved_intent():
    rt = _rt()
    qs = rt.initial_quota_state()
    s = rt.new_session("wh:1:1", {"uid": "u-1001"})
    rt.start(s, qs)
    step = rt.submit(s, "p1", {"q1": "yes"}, qs)
    hooks = step.get("webhooks")
    assert hooks and len(hooks) == 1
    h = hooks[0]
    assert h["method"] == "POST"
    assert h["url"] == "https://hook.example/x?uid=u-1001"          # url merge token resolved
    assert h["payload"] == {"answer": "Yes", "uid": "u-1001"}        # payload tokens resolved (label)


def test_webhook_intent_carries_stable_idempotency_key():
    # a delivering caller (or its receiver) must be able to dedupe under retry
    rt = _rt()
    qs = rt.initial_quota_state()
    s = rt.new_session("wh:1:idem", {"uid": "1"})
    rt.start(s, qs)
    h = rt.submit(s, "p1", {"q1": "yes"}, qs)["webhooks"][0]
    assert h.get("idempotency_key")
    # same (session, action) -> same key; a different session -> a different key
    s2 = rt.new_session("wh:1:other", {"uid": "1"})
    rt.start(s2, qs)
    h2 = rt.submit(s2, "p1", {"q1": "yes"}, qs)["webhooks"][0]
    assert h["idempotency_key"] != h2["idempotency_key"]


def test_webhook_fires_once():
    rt = _rt()
    qs = rt.initial_quota_state()
    s = rt.new_session("wh:1:2", {"uid": "1"})
    rt.start(s, qs)
    rt.submit(s, "p1", {"q1": "yes"}, qs)                  # fires here
    step2 = rt.submit(s, "p2", {"q2": "hello"}, qs)        # q1 still 'yes' but already fired
    assert "webhooks" not in step2


def test_webhook_does_not_fire_when_condition_false():
    rt = _rt()
    qs = rt.initial_quota_state()
    s = rt.new_session("wh:1:3", {"uid": "1"})
    rt.start(s, qs)
    step = rt.submit(s, "p1", {"q1": "no"}, qs)
    assert "webhooks" not in step


def test_deliver_no_url_is_safe():
    assert deliver({"url": "", "payload": {}}) is False


# ----------------------------------------------------------------- HMAC signing
from survey_agent.integrations import webhook as wh


def test_sign_verify_roundtrip():
    body = b'{"event":"qualified_lead","uid":"7"}'
    _, header = wh.sign("topsecret", body)
    assert wh.verify("topsecret", body, header)
    assert not wh.verify("wrong-secret", body, header)         # bad key -> reject
    assert not wh.verify("topsecret", body + b"x", header)      # tampered body -> reject


def test_verify_rejects_stale_timestamp():
    body = b"{}"
    _, header = wh.sign("s", body, ts=1)                        # ancient timestamp
    assert not wh.verify("s", body, header, tolerance=300)


def test_deliver_signs_when_secret_set(monkeypatch):
    captured = {}

    class _Resp:
        def read(self): return b""

    def fake_urlopen(req, timeout=5):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(wh.urllib.request, "urlopen", fake_urlopen)
    intent = {"url": "https://hook.example/x", "method": "POST",
              "payload": {"event": "qualified_lead", "uid": "7"}}
    assert wh.deliver(intent, secret="shh") is True
    sig = captured["headers"].get("x-survey-signature")
    assert sig and sig.startswith("t=")
    assert wh.verify("shh", captured["body"], sig)              # the receiver would accept it

    captured.clear()
    assert wh.deliver(intent) is True                           # no secret -> no signature header
    assert "x-survey-signature" not in captured["headers"]
