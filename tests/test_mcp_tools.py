"""The connector (MCP) parity tools — an LLM operating over MCP gets the analysis + lifecycle verbs,
and marking a survey LIVE is gated exactly like launch_survey (an agent can't field on its own)."""
import importlib

import pytest

pytest.importorskip("mcp")        # the connector tools need the `mcp` package; skip if not installed


def _spec():
    return {"survey": {"id": "m1", "version": 1, "title": "M1", "tags": ["t"],
                       "settings": {"url_variables": ["uid"]},
                       "pages": [{"id": "p1", "questions": [
                           {"id": "q1", "type": "radio", "required": True, "title": "Use it?",
                            "options": [{"value": "y", "label": "Yes"}, {"value": "n", "label": "No"}]}]}],
                       "actions": [{"type": "disqualify", "reason_code": "non_user",
                                    "when": {"ref": {"kind": "question", "id": "q1"}, "operator": "equals", "value": "n"}},
                                   {"type": "complete", "redirect": {"kind": "default"}}]}}


def _server(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))       # read at import time → reload after setting it
    monkeypatch.setenv("SURVEY_STORE", "memory")
    monkeypatch.setenv("SURVEY_LLM", "stub")
    monkeypatch.delenv("SURVEY_LAUNCH_APPROVAL_TOKEN", raising=False)
    import survey_agent.mcp.server as server
    importlib.reload(server)
    return server


def test_pretest_insights_export_tools(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    spec = _spec()
    pt = server.pretest_survey(survey_json=spec, n=8)
    assert pt["n"] == 8 and 0 <= pt["incidence"] <= 1 and isinstance(pt["reasons"], dict)
    ins = server.survey_insights(survey_json=spec, simulate=20)
    assert ins["engine"] == "stub" and "findings" in ins and ins["n"] == 20
    csv = server.export_survey(survey_json=spec, simulate=5, format="csv")
    assert csv["n"] == 5 and csv["csv"].startswith("id,")
    js = server.export_survey(survey_json=spec, simulate=5, format="json")
    assert js["format"] == "json" and len(js["rows"]) == 5 and js["columns"]


def test_set_survey_status_sets_freely_but_gates_live(tmp_path, monkeypatch):
    server = _server(tmp_path, monkeypatch)
    server.save_survey(_spec())
    assert server.set_survey_status(survey="m1", project="Northwind")["project"] == "Northwind"   # move: free
    assert server.set_survey_status(survey="m1", status="approved")["state"] == "approved"      # non-live: free
    blocked = server.set_survey_status(survey="m1", status="live")
    assert blocked["ok"] is False and blocked["live"] is False and "responsibilities" in blocked
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "sek")
    okk = server.set_survey_status(survey="m1", status="live",
                                   approval_token="sek", acknowledge_responsibilities=True)
    assert okk["ok"] and okk["state"] == "live" and okk["live"] is True
