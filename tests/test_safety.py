"""Guards on survey-authored strings that reach an interpreter: SQL identifiers in enrichment specs,
regexes in logic / validation, and the pluggable enrichment-provider seam."""
import sys
import time
import types

import pytest

from survey_agent.engine import safety, validate
from survey_agent.engine.ast_eval import EvalContext, evaluate
from survey_agent.engine.runtime import SurveyRuntime
from survey_agent.enrich import StaticProvider, get_provider
from survey_agent.enrich import spec as spec_mod
from survey_agent.enrich.firebolt_sql import compile_detect_sql, compile_value_sql

EVIL = "search_term) OR 1=1 --"
CATASTROPHIC = "^(a+)+$"


def _spec(**over):
    s = {"id": "x", "event_type": "web_search", "window": {"last": "30d"},
         "criteria": {"op": "and", "conditions": [
             {"field": "search_term", "operator": "contains", "value": "netflix"}]},
         "operation": "latest", "field": "search_term"}
    s.update(over)
    return s


def _survey(settings=None, validation=None, logic=None):
    q = {"id": "q", "type": "text"}
    if validation:
        q["validation"] = validation
    q2 = {"id": "q2", "type": "radio", "options": [{"value": "a"}]}
    if logic:
        q2["display_logic"] = logic
    return {"survey": {"id": "t", "title": "T", "settings": settings or {},
                       "pages": [{"id": "p1", "questions": [q]}, {"id": "p2", "questions": [q2]}],
                       "actions": [{"type": "complete"}]}}


# ----------------------------------------------------------------- SQL identifiers
def test_identifier_pattern():
    assert safety.is_identifier("search_term") and safety.is_identifier("_x1")
    for bad in ("1x", "a-b", "a b", "a;b", "a.b", "", None, EVIL, 'x"'):
        assert not safety.is_identifier(bad)
    assert safety.is_table_name("analytics.search_events")
    assert not safety.is_table_name("a.b.c") and not safety.is_table_name("t; drop")


def test_compile_rejects_injected_operation_field():
    with pytest.raises(ValueError):
        compile_value_sql(_spec(field=EVIL), "u-1001")


@pytest.mark.parametrize("over", [
    {"criteria": {"field": EVIL, "operator": "equals", "value": "x"}},
    {"criteria": {"op": "or", "conditions": [{"field": "url) --", "operator": "is_set"}]}},
])
def test_compile_rejects_injected_identifiers(over):
    with pytest.raises(ValueError):
        compile_value_sql(_spec(**over), "u-1001")
    with pytest.raises(ValueError):
        compile_detect_sql(_spec(**over))


def test_compile_rejects_fields_outside_the_catalog_allow_list():
    with pytest.raises(ValueError, match="not a declared field"):
        compile_value_sql(_spec(field="password_hash"), "u-1001")


def test_compile_rejects_unknown_event_type():
    with pytest.raises(ValueError, match="unknown event_type"):
        compile_value_sql(_spec(event_type="nope"), "u-1001")


def test_values_stay_bound_parameters():
    s = _spec(criteria={"field": "search_term", "operator": "equals", "value": "x' OR '1'='1"})
    sql, params = compile_value_sql(s, "u-1001")
    assert "x' OR" not in sql and params["c"] == "x' OR '1'='1"


def test_respondent_id_column_is_configurable(monkeypatch):
    sql, _ = compile_value_sql(_spec(), "u-1001")
    assert "respondent_id = %(uid)s" in sql
    monkeypatch.setitem(spec_mod.EVENT_TYPES["web_search"], "id_field", "user_id")
    sql, _ = compile_detect_sql(_spec(operation="exists"))
    assert "SELECT DISTINCT user_id" in sql and "GROUP BY user_id" in sql
    monkeypatch.setitem(spec_mod.EVENT_TYPES["web_search"], "id_field", "user_id; --")
    with pytest.raises(ValueError):
        compile_detect_sql(_spec(operation="exists"))


def test_validator_flags_unsafe_enrichment_identifiers():
    bad = _spec(field=EVIL, criteria={"field": "a b", "operator": "regex", "value": CATASTROPHIC})
    errs = validate.semantic_errors(_survey(settings={"enrichments": [bad]}))
    assert any("field" in e and "plain identifier" in e for e in errs)
    assert any("criteria field 'a b'" in e for e in errs)
    assert any("criteria regex" in e for e in errs)
    good = validate.semantic_errors(_survey(settings={"enrichments": [_spec()]}))
    assert not [e for e in good if "enrichments" in e]


def test_validator_skips_llm_enrichments():
    llm = {"id": "s", "source": "llm", "inputs": ["q"], "field": "not an identifier"}
    assert not [e for e in validate.semantic_errors(_survey(settings={"enrichments": [llm]}))
                if "enrichments" in e]


# ----------------------------------------------------------------- regex bounds
@pytest.mark.parametrize("pattern", ["(x+)+", "(x*)*", "(x+)*", r"(\w+\s?)*$", "(?:a+){2,}"])
def test_regex_problem_flags_nested_quantifiers(pattern):
    assert "nests unbounded" in safety.regex_problem(pattern)


@pytest.mark.parametrize("pattern", [r"^\d{5}$", "(ab)+c", "(a|b)+", r"^[A-Z]{2}\d+$", "a{1,40}"])
def test_regex_problem_allows_ordinary_patterns(pattern):
    assert safety.regex_problem(pattern) is None


def test_regex_problem_length_and_syntax():
    assert "max 200" in safety.regex_problem("a" * 201)
    assert "invalid regex" in safety.regex_problem("(")
    assert safety.regex_problem(5) == "regex must be a string"


def test_safe_search_caps_input_length():
    text = "a" * (safety.MAX_REGEX_INPUT + 10) + "TAIL"
    assert safety.safe_search("TAIL", text) is False      # past the cap — never scanned
    assert safety.safe_search("^a", text) is True
    assert safety.safe_search(CATASTROPHIC, "aaaa") is None


def test_validator_rejects_unsafe_validation_regex_and_regex_match():
    errs = validate.semantic_errors(_survey(validation={"regex": CATASTROPHIC}))
    assert any("validation.regex" in e for e in errs)
    errs = validate.semantic_errors(_survey(validation={"regex": "x" * 300}))
    assert any("validation.regex" in e and "max 200" in e for e in errs)
    logic = {"ref": {"kind": "question", "id": "q"}, "operator": "regex_match", "value": CATASTROPHIC}
    errs = validate.semantic_errors(_survey(logic=logic))
    assert any("regex_match" in e for e in errs)
    ok = {"ref": {"kind": "question", "id": "q"}, "operator": "regex_match", "value": "^yes"}
    assert not [e for e in validate.semantic_errors(_survey(logic=ok)) if "regex" in e]


def test_regex_match_is_bounded_and_fails_closed():
    node = {"ref": {"kind": "question", "id": "q"}, "operator": "regex_match", "value": CATASTROPHIC}
    t0 = time.monotonic()
    assert evaluate(node, EvalContext(answers={"q": "a" * 5000 + "!"})) is False
    assert time.monotonic() - t0 < 1.0
    ok = {"ref": {"kind": "question", "id": "q"}, "operator": "regex_match", "value": "^ab"}
    assert evaluate(ok, EvalContext(answers={"q": "abc"})) is True


def test_runtime_validation_regex_is_bounded():
    spec = _survey(validation={"regex": r"^\d{5}$", "regex_message": "zip!"})
    rt = SurveyRuntime(spec)
    page = spec["survey"]["pages"][0]
    s = rt.new_session("t:1:1", {})
    assert rt.validate_answers(page, {"q": "12345"}, s) == {}
    assert rt.validate_answers(page, {"q": "1234"}, s) == {"q": "zip!"}
    # a pattern the validator would reject is skipped at runtime rather than hanging/trapping
    spec2 = _survey(validation={"regex": CATASTROPHIC})
    rt2 = SurveyRuntime(spec2)
    page = spec2["survey"]["pages"][0]
    t0 = time.monotonic()
    assert rt2.validate_answers(page, {"q": "a" * 5000 + "!"}, rt2.new_session("t:1:2", {})) == {}
    assert time.monotonic() - t0 < 1.0


def test_enrichment_regex_criteria_is_bounded():
    s = _spec(operation="count", window=None, criteria={"field": "search_term", "operator": "regex", "value": CATASTROPHIC})
    events = [{"search_term": "a" * 5000 + "!"}]
    assert spec_mod.evaluate(s, events, None) == 0


# ----------------------------------------------------------------- pluggable provider seam
class _MyProvider:
    def __init__(self, **kw):
        self.kw = kw

    def resolve(self, uid, specs):
        return {s["id"]: f"custom:{uid}" for s in specs}


def test_get_provider_loads_import_path(monkeypatch):
    mod = types.ModuleType("my_enrich_plugin")
    mod.MyProvider = _MyProvider
    monkeypatch.setitem(sys.modules, "my_enrich_plugin", mod)
    p = get_provider("my_enrich_plugin:MyProvider", region="eu")
    assert isinstance(p, _MyProvider) and p.kw == {"region": "eu"}
    assert isinstance(get_provider("my_enrich_plugin.MyProvider"), _MyProvider)
    monkeypatch.setenv("SURVEY_ENRICH", "my_enrich_plugin:MyProvider")
    assert isinstance(get_provider(), _MyProvider)


def test_get_provider_builtins_and_bad_names():
    assert isinstance(get_provider("static"), StaticProvider)
    with pytest.raises(ValueError, match="import path"):
        get_provider("nonsense")
    with pytest.raises(ImportError):
        get_provider("no_such_pkg_xyz:Provider")
