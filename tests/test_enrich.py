import json
import os
from datetime import datetime, timedelta, timezone

from survey_agent.enrich import spec as spec_mod
from survey_agent.enrich import StaticProvider, resolve_for_survey
from survey_agent.enrich.firebolt_sql import compile_value_sql, compile_detect_sql

SURVEY = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")
NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def ev(term, days_ago):
    return {"search_term": term, "event_time": (NOW - timedelta(days=days_ago)).isoformat()}


STREAM_CRITERIA = {"op": "or", "conditions": [
    {"field": "search_term", "operator": "contains_any", "value": ["netflix", "disney", "what to watch"]}]}


def test_count_with_window_and_criteria():
    events = [ev("netflix reviews", 2), ev("disney cost", 5), ev("what to watch this weekend", 40), ev("running shoes", 1)]
    spec = {"id": "x", "event_type": "web_search", "window": {"last": "30d"},
            "criteria": STREAM_CRITERIA, "operation": "count", "field": "search_term"}
    # the 40-day-old what to watch search falls outside the 30d window
    assert spec_mod.evaluate(spec, events, NOW) == 2


def test_latest_returns_most_recent_matching():
    events = [ev("netflix reviews", 2), ev("disney cost", 5)]
    spec = {"id": "x", "event_type": "web_search", "window": {"last": "30d"},
            "criteria": STREAM_CRITERIA, "operation": "latest", "field": "search_term"}
    assert spec_mod.evaluate(spec, events, NOW) == "netflix reviews"


def test_sum_duration():
    events = [
        {"app_category": "Entertainment", "minutes": 62, "event_time": (NOW - timedelta(days=1)).isoformat()},
        {"app_category": "Entertainment", "minutes": 40, "event_time": (NOW - timedelta(days=3)).isoformat()},
        {"app_category": "Social", "minutes": 200, "event_time": (NOW - timedelta(days=1)).isoformat()},
    ]
    spec = {"id": "m", "event_type": "app_usage", "window": {"last": "7d"},
            "criteria": {"field": "app_category", "operator": "equals", "value": "Entertainment"},
            "operation": "sum", "field": "minutes"}
    assert spec_mod.evaluate(spec, events, NOW) == 102


def test_exists_and_distinct_count():
    events = [ev("netflix", 1), ev("netflix", 2), ev("disney", 3)]
    base = {"event_type": "web_search", "window": {"last": "30d"}, "criteria": STREAM_CRITERIA, "field": "search_term"}
    assert spec_mod.evaluate({**base, "id": "e", "operation": "exists"}, events, NOW) is True
    assert spec_mod.evaluate({**base, "id": "d", "operation": "distinct_count"}, events, NOW) == 2


def test_static_provider_resolves_survey_enrichments():
    with open(SURVEY) as f:
        survey = json.load(f)["survey"]
    provider = StaticProvider()  # loads enrichments/_demo_events.json
    vals = resolve_for_survey(survey, "u-1001", provider)
    assert vals["stream_search_count_30d"] == 3
    assert vals["recent_stream_search"] == "netflix price increase"
    assert vals["ent_app_minutes_7d"] == 145  # 62 + 35 + 48 (entertainment apps within 7d)


def test_behavioral_resolve_skips_llm_specs():
    # the behavioral path must ignore `source: llm` specs (handled separately at completion)
    import survey_agent.ai as ai
    from survey_agent.enrich import resolve_for_survey, StaticProvider
    with open(SURVEY) as f:
        survey = json.load(f)["survey"]
    vals = resolve_for_survey(survey, "u-1001", StaticProvider())
    assert "why_sentiment" not in vals
    assert set(vals) == {"recent_stream_search", "stream_search_count_30d", "ent_app_minutes_7d"}


def test_llm_is_fail_closed_without_optin():
    import survey_agent.ai as ai
    from survey_agent.enrich import resolve_llm_for_session
    with open(SURVEY) as f:
        survey = json.load(f)["survey"]
    session = {"session_id": "s", "survey_id": "x", "answers": {"q_why_search": "great, very helpful"}}
    values, audit = resolve_llm_for_session(survey, session, llm=ai.StubLLM(), enabled=False)
    assert values == {}                                   # nothing sent to the LLM
    assert audit and audit[0]["status"] == "skipped_not_opted_in"   # but it's recorded


def test_llm_runs_when_opted_in_consented_and_is_audited():
    import survey_agent.ai as ai
    from survey_agent.enrich import resolve_llm_for_session
    with open(SURVEY) as f:
        survey = json.load(f)["survey"]
    session = {"session_id": "s", "survey_id": "x",
               "answers": {"q_consent": "yes", "q_why_search": "I had bad nausea side effects"}}
    values, audit = resolve_llm_for_session(survey, session, llm=ai.StubLLM(), enabled=True)
    assert values["why_sentiment"] == "negative"          # stub sentiment on negative text
    rec = next(a for a in audit if a["id"] == "why_sentiment")
    assert rec["status"] == "ok" and rec["inputs"] == ["q_why_search"] and rec["model"] == "stub"


def test_llm_skipped_without_respondent_consent():
    import survey_agent.ai as ai
    from survey_agent.enrich import resolve_llm_for_session
    with open(SURVEY) as f:
        survey = json.load(f)["survey"]
    # opted in at deployment level, but the respondent did NOT consent (q_consent != yes)
    session = {"session_id": "s", "survey_id": "x", "answers": {"q_why_search": "great stuff"}}
    values, audit = resolve_llm_for_session(survey, session, llm=ai.StubLLM(), enabled=True)
    assert "why_sentiment" not in values
    assert next(a for a in audit if a["id"] == "why_sentiment")["status"] == "skipped_no_respondent_consent"


def test_compile_value_sql_shapes():
    spec = {"id": "x", "event_type": "web_search", "window": {"last": "30d"},
            "criteria": STREAM_CRITERIA, "operation": "latest", "field": "search_term"}
    sql, params = compile_value_sql(spec, "u-1001")
    assert "search_events" in sql
    assert "ORDER BY event_time DESC LIMIT 1" in sql
    assert "INTERVAL '30' DAY" in sql
    assert params["uid"] == "u-1001"


def test_compile_detect_sql_for_sidecar():
    spec = {"id": "t", "event_type": "web_search", "window": {"last": "1d"},
            "criteria": STREAM_CRITERIA, "operation": "exists"}
    sql, params = compile_detect_sql(spec, since="2026-06-01 00:00:00")
    assert "SELECT DISTINCT respondent_id" in sql
    assert "event_time > %(since)s" in sql
    assert params["since"] == "2026-06-01 00:00:00"
