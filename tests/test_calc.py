from survey_agent.engine.ast_eval import EvalContext
from survey_agent.engine import calc


def ctx(**kw):
    return EvalContext(**kw)


def test_score_single_and_multi():
    c = ctx(answers={"q": "current"})
    spec = {"op": "score", "ref": {"kind": "question", "id": "q"}, "map": {"current": 3, "never": 0}}
    assert calc.evaluate(spec, c) == 3
    c2 = ctx(answers={"q": ["a", "b"]})
    spec2 = {"op": "score", "ref": {"kind": "question", "id": "q"}, "map": {"a": 2, "b": 5}}
    assert calc.evaluate(spec2, c2) == 7


def test_arithmetic_and_refs():
    c = ctx(answers={"x": "3"}, variables={"y": 4})
    add = {"op": "add", "args": [{"op": "ref", "ref": {"kind": "question", "id": "x"}},
                                 {"op": "ref", "ref": {"kind": "variable", "id": "y"}},
                                 {"op": "const", "value": 1}]}
    assert calc.evaluate(add, c) == 8
    assert calc.evaluate({"op": "avg", "args": [{"op": "const", "value": 2}, {"op": "const", "value": 4}]}, c) == 3


def test_count_selected():
    c = ctx(answers={"q": ["a", "b", "c"]})
    assert calc.evaluate({"op": "count_selected", "ref": {"kind": "question", "id": "q"}}, c) == 3


def test_bucket():
    c = ctx(variables={"score": 3})
    spec = {"op": "bucket", "of": {"op": "ref", "ref": {"kind": "variable", "id": "score"}},
            "ranges": [[3, 3, "active"], [1, 2, "lapsed"], [0, 0, "none"]], "default": "?"}
    assert calc.evaluate(spec, c) == "active"
    assert calc.evaluate(spec, ctx(variables={"score": 2})) == "lapsed"
    assert calc.evaluate(spec, ctx(variables={"score": 9})) == "?"


def test_whole_numbers_stay_int():
    c = ctx()
    assert calc.evaluate({"op": "sum", "args": [{"op": "const", "value": 1}, {"op": "const", "value": 2}]}, c) == 3
    assert isinstance(calc.evaluate({"op": "sum", "args": [{"op": "const", "value": 1}]}, c), int)
