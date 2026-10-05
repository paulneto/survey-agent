import json
import os

from survey_agent.engine import validate

SURVEY = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")


def load():
    with open(SURVEY) as f:
        return json.load(f)


def test_example_passes_semantic_lint():
    assert validate.semantic_errors(load()) == []


def test_example_passes_full_validate_modulo_optional_jsonschema():
    # validate() may include a single "[warn] jsonschema not installed" line if the lib is absent;
    # there must be no real errors.
    errs = [e for e in validate.validate(load()) if not e.startswith("[warn]")]
    assert errs == []


def _minimal():
    return {"survey": {"id": "t", "title": "T",
                       "pages": [{"id": "p", "questions": [
                           {"id": "q", "type": "radio", "options": [{"value": "a"}]}]}],
                       "actions": [{"type": "complete"}]}}


def test_tightened_schema_rejects_unknown_survey_and_settings_keys():
    # additionalProperties:false makes `validate` a real gate — typos no longer pass silently.
    spec = _minimal()
    spec["survey"]["flavour"] = "extra"                 # unknown top-level key
    spec["survey"]["settings"] = {"theem": "paper"}      # typo'd settings key
    errs = [e for e in validate.validate(spec) if not e.startswith("[warn]")]
    assert any("flavour" in e for e in errs)
    assert any("theem" in e for e in errs)


def test_tightened_schema_rejects_unknown_validation_key():
    spec = _minimal()
    spec["survey"]["pages"][0]["questions"][0]["validation"] = {"maxlen": 5}   # should be max_length
    errs = [e for e in validate.validate(spec) if not e.startswith("[warn]")]
    assert any("maxlen" in e for e in errs)


def test_minimal_survey_still_valid():
    assert [e for e in validate.validate(_minimal()) if not e.startswith("[warn]")] == []


def test_unknown_question_ref_is_caught():
    spec = load()
    spec["survey"]["actions"].append({
        "type": "disqualify",
        "when": {"ref": {"kind": "question", "id": "q_does_not_exist"}, "operator": "equals", "value": "x"},
    })
    errs = validate.semantic_errors(spec)
    assert any("q_does_not_exist" in e for e in errs)


def test_pathologically_nested_logic_is_flagged_not_crashed():
    # A runaway-nested condition (e.g. a model emitting deep and/or) would RecursionError while the
    # engine evaluates it at field time. The validator catches it at the gate as a clean error.
    spec = _minimal()
    node = {"ref": {"kind": "question", "id": "q"}, "operator": "equals", "value": "a"}
    for _ in range(80):
        node = {"operator": "and", "conditions": [node]}
    spec["survey"]["actions"].append({"type": "disqualify", "reason_code": "deep", "when": node})
    errs = validate.semantic_errors(spec)
    assert any("nests deeper" in e for e in errs)


def test_normal_nesting_depth_is_not_flagged():
    # a handful of levels (realistic) must pass — the guard only trips on pathological depth.
    spec = _minimal()
    node = {"ref": {"kind": "question", "id": "q"}, "operator": "equals", "value": "a"}
    for _ in range(5):
        node = {"operator": "and", "conditions": [node]}
    spec["survey"]["actions"].append({"type": "disqualify", "reason_code": "ok", "when": node})
    assert not any("nests deeper" in e for e in validate.semantic_errors(spec))


def test_missing_complete_action_is_caught():
    spec = load()
    spec["survey"]["actions"] = [a for a in spec["survey"]["actions"] if a.get("type") != "complete"]
    errs = validate.semantic_errors(spec)
    assert any("no 'complete' action" in e for e in errs)


def test_skip_to_unknown_page_is_caught():
    spec = load()
    spec["survey"]["actions"].append({
        "type": "skip", "to_page": "nope",
        "when": {"ref": {"kind": "question", "id": "q_age"}, "operator": "greater_than", "value": 90},
    })
    assert any("nope" in e for e in validate.semantic_errors(spec))


def test_undeclared_url_var_is_caught():
    spec = load()
    spec["survey"]["pages"][0]["display_logic"] = {
        "ref": {"kind": "url_var", "id": "undeclared_param"}, "operator": "equals", "value": "x"
    }
    assert any("undeclared_param" in e for e in validate.semantic_errors(spec))


def test_duplicate_question_id_is_caught():
    # answers live in one flat dict, so a reused id silently overwrites — the lint must catch it.
    spec = _minimal()
    spec["survey"]["pages"][0]["questions"].append(
        {"id": "q", "type": "radio", "options": [{"value": "b"}]})   # reuses id 'q'
    assert any("duplicate question id 'q'" in e for e in validate.semantic_errors(spec))


def test_choice_question_without_options_is_caught():
    spec = _minimal()
    spec["survey"]["pages"][0]["questions"] = [{"id": "q", "type": "radio"}]   # no options/options_source
    assert any("q" in e and "no options" in e for e in validate.semantic_errors(spec))


def test_grid_question_without_rows_is_caught():
    spec = _minimal()
    spec["survey"]["pages"][0]["questions"] = [
        {"id": "g", "type": "grid", "options": [{"value": "a"}]}]   # columns but no rows
    assert any("grid" in e and "no rows" in e for e in validate.semantic_errors(spec))


def test_set_variable_without_name_is_caught():
    # schema-valid (only `type` is required) but crashes the engine at submit — lint must catch it.
    spec = _minimal()
    spec["survey"]["actions"].append({"type": "set_variable", "value": "x"})   # no name
    assert any("set_variable" in e and "name" in e for e in validate.semantic_errors(spec))


def test_webhook_payload_token_to_unknown_question_is_caught():
    # a typo'd {{q:…}} in a payload silently renders empty at field time — the lint must catch it.
    spec = _minimal()
    spec["survey"]["settings"] = {"url_variables": ["uid"]}
    spec["survey"]["actions"].append({
        "type": "webhook", "url": "https://hook.example/x?uid={{url:uid}}",
        "payload": {"a": "{{q:q_typo}}"},
        "when": {"ref": {"kind": "question", "id": "q"}, "operator": "equals", "value": "a"}})
    errs = validate.semantic_errors(spec)
    assert any("q_typo" in e and "payload" in e for e in errs)


def test_webhook_url_token_to_undeclared_url_var_is_caught():
    spec = _minimal()
    spec["survey"]["actions"].append({
        "type": "webhook", "url": "https://hook.example/x?g={{url:not_declared}}", "payload": {},
        "when": {"ref": {"kind": "question", "id": "q"}, "operator": "equals", "value": "a"}})
    assert any("not_declared" in e for e in validate.semantic_errors(spec))


def test_redirect_url_token_to_unknown_question_is_caught():
    spec = _minimal()
    spec["survey"]["actions"] = [
        {"type": "complete", "redirect": {"kind": "url", "url": "https://t.example/?x={{q:nope}}"}}]
    assert any("nope" in e and "redirect" in e for e in validate.semantic_errors(spec))


def test_hidden_calc_ref_to_unknown_question_is_caught():
    # the calc AST is data too — a ref to a question that doesn't exist must not pass the gate.
    spec = _minimal()
    spec["survey"]["hidden"] = [
        {"id": "score", "calc": {"op": "score", "ref": {"kind": "question", "id": "q_missing"},
                                 "map": {"a": 1}}}]
    assert any("q_missing" in e and "calc" in e for e in validate.semantic_errors(spec))


def test_var_token_to_undefined_variable_is_caught():
    spec = _minimal()
    spec["survey"]["actions"] = [
        {"type": "complete", "redirect": {"kind": "url", "url": "https://t.example/?s={{var:undef}}"}}]
    assert any("undef" in e for e in validate.semantic_errors(spec))


def test_valid_calc_and_tokens_pass():
    # the shipped streaming survey exercises var:/behavior: tokens + hidden calc — must stay clean.
    assert validate.semantic_errors(load()) == []
