from survey_agent.engine.ast_eval import EvalContext, evaluate


def ctx(**kw):
    return EvalContext(**kw)


def cond(qid, op, value=None):
    c = {"ref": {"kind": "question", "id": qid}, "operator": op}
    if value is not None:
        c["value"] = value
    return c


def test_empty_is_true():
    assert evaluate({}, ctx()) is True
    assert evaluate(None, ctx()) is True


def test_between_normal_and_malformed_bounds():
    c = ctx(answers={"age": 30})
    assert evaluate(cond("age", "between", [18, 65]), c)
    assert not evaluate(cond("age", "between", [40, 65]), c)
    # malformed bounds must fail closed, never raise (was a crash: `lo, hi = expected`)
    for bad in (40, "x", [18], [1, 2, 3], None):
        assert evaluate(cond("age", "between", bad), c) is False


def test_equals_scalar_and_list():
    c = ctx(answers={"q": "current"})
    assert evaluate(cond("q", "equals", "current"), c)
    assert not evaluate(cond("q", "equals", "never"), c)
    c2 = ctx(answers={"q": ["a", "b"]})
    assert evaluate(cond("q", "equals", ["b", "a"]), c2)
    assert not evaluate(cond("q", "equals", ["a"]), c2)


def test_equals_ordered_is_order_sensitive_for_rankings():
    # `equals` is set-based (right for checkbox); `equals_ordered` respects order (right for ranking).
    c = ctx(answers={"rank": ["a", "b", "c"]})
    assert evaluate(cond("rank", "equals", ["c", "b", "a"]), c)            # set-based: order ignored
    assert not evaluate(cond("rank", "equals_ordered", ["c", "b", "a"]), c)  # order matters
    assert evaluate(cond("rank", "equals_ordered", ["a", "b", "c"]), c)
    # duplicates also matter for ordered equality
    c2 = ctx(answers={"rank": ["a", "a", "b"]})
    assert not evaluate(cond("rank", "equals_ordered", ["a", "b"]), c2)
    # scalar behaves like equals
    assert evaluate(cond("q", "equals_ordered", "x"), ctx(answers={"q": "x"}))


def test_contains_and_one_of():
    c = ctx(answers={"q": ["netflix", "disney"]})
    assert evaluate(cond("q", "contains", "disney"), c)
    assert not evaluate(cond("q", "contains", "max"), c)
    assert evaluate(cond("q", "is_one_of", ["max", "netflix"]), c)
    assert not evaluate(cond("q", "is_one_of", ["max"]), c)
    assert evaluate(cond("q", "is_not_one_of", ["max"]), c)


def test_text_contains_substring():
    c = ctx(answers={"q": "I love Netflix"})
    assert evaluate(cond("q", "contains", "Netflix"), c)


def test_numeric_with_string_coercion():
    c = ctx(answers={"age": "34"})
    assert evaluate(cond("age", "greater_than", 18), c)
    assert evaluate(cond("age", "less_than", 99), c)
    assert evaluate(cond("age", "between", [18, 65]), c)
    assert not evaluate(cond("age", "between", [40, 50]), c)
    assert evaluate(cond("age", "greater_or_equal", 34), c)


def test_answered_and_blank_semantics():
    answered = ctx(answers={"q": "x"})
    blank = ctx(answers={"q": ""})
    missing = ctx(answers={})
    assert evaluate(cond("q", "is_answered"), answered)
    assert not evaluate(cond("q", "is_answered"), blank)
    assert evaluate(cond("q", "is_not_answered"), missing)
    # a comparison against a blank/missing value is False (logic only fires with data)
    assert not evaluate(cond("q", "equals", "x"), blank)
    assert not evaluate(cond("q", "not_equals", "x"), missing)


def test_and_or_groups():
    c = ctx(answers={"use": "current", "age": 40})
    g = {"op": "and", "conditions": [cond("use", "equals", "current"), cond("age", "greater_than", 18)]}
    assert evaluate(g, c)
    g2 = {"op": "or", "conditions": [cond("use", "equals", "never"), cond("age", "greater_than", 18)]}
    assert evaluate(g2, c)
    g3 = {"op": "and", "conditions": [cond("use", "equals", "never"), cond("age", "greater_than", 18)]}
    assert not evaluate(g3, c)


def test_url_var_and_variable_refs():
    c = ctx(url_vars={"group": "vip"}, variables={"segment": "streamer_current"})
    assert evaluate({"ref": {"kind": "url_var", "id": "group"}, "operator": "equals", "value": "vip"}, c)
    assert evaluate({"ref": {"kind": "variable", "id": "segment"}, "operator": "equals", "value": "streamer_current"}, c)


def test_quota_is_full():
    c = ctx(quotas={"q1": {"count": 300, "limit": 300}, "q2": {"count": 5, "limit": 10}})
    assert evaluate({"ref": {"kind": "quota", "id": "q1"}, "operator": "is_full"}, c)
    assert not evaluate({"ref": {"kind": "quota", "id": "q2"}, "operator": "is_full"}, c)
    assert evaluate({"ref": {"kind": "quota", "id": "q2"}, "operator": "is_not_full"}, c)
