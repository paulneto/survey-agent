import json
import os

import pytest

from survey_agent.engine.runtime import SurveyRuntime
from survey_agent import report

SURVEY = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")


@pytest.fixture
def rt():
    with open(SURVEY) as f:
        return SurveyRuntime(json.load(f))


def test_simulation_collects_rows_and_quotas(rt):
    rows, quotas = report.collect_via_simulation(rt, n=120, seed=3)
    assert len(rows) == 120
    assert all(r["status"] in ("completed", "disqualified", "overquota", "partial") for r in rows)
    assert "quota_current" in quotas


def test_build_report_has_funnel_questions_derived(rt):
    rows, quotas = report.collect_via_simulation(rt, n=200, seed=5)
    rep = report.build_report(rt, rows, quotas)
    assert rep["n"] == 200
    assert sum(rep["funnel"]["counts"]) == 200
    assert 0 <= rep["incidence"] <= 100
    qids = {q["id"] for q in rep["questions"]}
    assert "q_service_use" in qids                 # a categorical question is charted
    assert all(q["id"] not in ("q_intro_video",) for q in rep["questions"])  # display-only excluded
    assert any(d["id"].startswith("stream_score") or d["id"] == "stream_score" for d in rep["derived"])


def test_render_html_is_self_contained(rt):
    rows, quotas = report.collect_via_simulation(rt, n=80, seed=1)
    html = report.render_html(report.build_report(rt, rows, quotas))
    assert "<!doctype html>" in html.lower()
    assert "cdn.jsdelivr.net/npm/chart.js" in html      # chart library via CDN (chosen)
    assert "Streaming Services Usage" in html
    assert "const R = {" in html                         # response data embedded inline


def test_nps_score_computed():
    spec = {"survey": {"id": "n", "title": "N", "pages": [{"id": "p", "questions": [
        {"id": "q_nps", "type": "nps", "scale_points": list(range(0, 11))}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}}
    rt = SurveyRuntime(spec)
    rows = [{"status": "completed", "reason_code": None, "answers": {"q_nps": v},
             "derived": {}, "group": "A", "loi": 60}
            for v in [10, 10, 9, 9, 8, 7, 6, 3, 0]]   # 3 promoters, 3 detractors of 9
    rep = report.build_report(rt, rows, {})
    q = next(q for q in rep["questions"] if q["id"] == "q_nps")
    assert "NPS =" in q["note"]
