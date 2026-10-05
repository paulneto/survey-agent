"""Closed-loop authoring: the simulator's structured coverage() is the agent's ground-truth
feedback signal, and `survey-agent author` repairs against it (not just schema errors).

These surveys are deliberately schema-AND-semantic VALID but runtime-broken — the semantic lint
checks refs/targets only, so a dead branch (a screen-out whose value never occurs, or a disqualify
that makes `complete` unreachable) slips past validation and is caught only by actually running the
engine on synthetic respondents. That distinction is the whole point of the loop."""
import json

import survey_agent.ai as ai
from survey_agent import cli, simulate
from survey_agent.engine import validate as v


def _survey(actions):
    return {"survey": {
        "id": "cov-test", "version": 1, "title": "Coverage test",
        "settings": {"url_variables": ["uid", "rid"], "theme": "paper"},
        "pages": [{"id": "p1", "questions": [
            {"id": "q_use", "type": "radio", "required": True, "title": "Do you use it?",
             "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]}],
        "actions": actions,
    }}


_COMPLETE = {"type": "complete", "redirect": {"kind": "default"}}


def _dq(reason, value, operator="equals"):
    return {"type": "disqualify", "reason_code": reason, "dq_message": "bye",
            "when": {"ref": {"kind": "question", "id": "q_use"}, "operator": operator, "value": value}}


def _is_valid(spec):
    return [e for e in v.validate(spec) if not e.startswith("[warn]")] == []


# ----------------------------------------------------------------- coverage() unit
def test_clean_survey_has_no_coverage_problems():
    spec = ai.StubLLM().author("screen coffee drinkers", v.load_schema())
    cov = simulate.coverage(spec, n=80)
    assert cov["problems"] == [], cov["problems"]
    assert cov["statuses"].get("completed", 0) > 0
    assert "no_consent" in cov["fired_reasons"]


def test_coverage_flags_unreachable_complete():
    # disqualify fires for EVERY answer -> nobody completes. Schema-valid, runtime-broken.
    spec = _survey([_dq("screened", ["yes", "no"], operator="is_one_of"), _COMPLETE])
    assert _is_valid(spec), "fixture should pass schema+semantic lint"
    cov = simulate.coverage(spec, n=60)
    completed_problem = next(p for p in cov["problems"] if "reached 'completed'" in p)
    # the message now pinpoints the blocking screen-out (the reason_code that caught everyone)
    assert "screened" in completed_problem, completed_problem


def test_coverage_flags_dead_screenout():
    # 'rare' is gated on a value that is not an option -> it can never fire (dead branch).
    spec = _survey([_dq("rare", "maybe"), _COMPLETE])
    assert _is_valid(spec), "fixture should pass schema+semantic lint"
    cov = simulate.coverage(spec, n=60)
    assert any("screen-out 'rare'" in p and "never triggered" in p for p in cov["problems"]), cov["problems"]


def _stuck_survey():
    # A required free-text answer whose regex no synthetic answer can satisfy -> the page never
    # advances. Schema + semantic VALID, but the engine can never reach a terminal: the coverage gate
    # must report this as a problem, not crash with KeyError('terminal').
    return {"survey": {
        "id": "stuck", "version": 1, "title": "Stuck",
        "settings": {"url_variables": ["uid", "rid"]},
        "pages": [{"id": "p1", "questions": [
            {"id": "q_code", "type": "text", "required": True, "title": "Code",
             "validation": {"regex": "^IMPOSSIBLE-[0-9]{40}$"}}]}],
        "actions": [_COMPLETE],
    }}


def test_coverage_flags_stuck_survey_instead_of_crashing():
    spec = _stuck_survey()
    assert _is_valid(spec), "fixture should pass schema+semantic lint"
    cov = simulate.coverage(spec, n=10)        # must not raise
    assert any("stuck" in p for p in cov["problems"]), cov["problems"]
    assert cov["statuses"].get("completed", 0) == 0   # nobody can finish — that's the point


# ----------------------------------------------------------------- numeric branch coverage
def _age_survey(min_age, max_age, dq_threshold):
    """One number question gated by a narrow `age less_than <threshold>` screen-out."""
    return {"survey": {
        "id": "age-test", "version": 1, "title": "Age test",
        "settings": {"url_variables": ["uid", "rid"], "theme": "paper"},
        "pages": [{"id": "p1", "questions": [
            {"id": "q_age", "type": "number", "required": True, "title": "Age?",
             "validation": {"min": min_age, "max": max_age}}]}],
        "actions": [
            {"type": "disqualify", "reason_code": "under_age", "dq_message": "bye",
             "when": {"ref": {"kind": "question", "id": "q_age"}, "operator": "less_than", "value": dq_threshold}},
            _COMPLETE],
    }}


def test_numeric_targets_straddle_thresholds():
    # a `less_than 18` on q_age yields the boundary set {17, 18, 19}; nested and/or is walked
    spec = _age_survey(16, 99, 18)
    assert simulate._numeric_targets(spec) == {"q_age": [17, 18, 19]}
    nested = {"op": "and", "conditions": [
        {"ref": {"kind": "question", "id": "q_age"}, "operator": "between", "value": [25, 40]}]}
    assert list(simulate._iter_conditions(nested))[0]["operator"] == "between"


def test_coverage_hits_narrow_numeric_screenout_at_low_n():
    # under_age is a ~1% window (only age 16 or 17 over a 16–99 range): a uniform random draw misses
    # it at n=30, but boundary-targeting must exercise it deterministically.
    spec = _age_survey(16, 99, 18)
    for seed in (7, 1, 42):
        cov = simulate.coverage(spec, n=30, seed=seed)
        assert "under_age" in cov["fired_reasons"], (seed, cov["reasons"])
        assert cov["problems"] == [], (seed, cov["problems"])


def test_out_of_validation_numeric_branch_stays_flagged():
    # screen-out needs age < 18 but the question's own min is 18 → no VALID answer triggers it.
    # That is a genuine dead branch and must stay flagged (targeting must not mask it).
    cov = simulate.coverage(_age_survey(18, 99, 18), n=80)
    assert any("screen-out 'under_age'" in p and "never triggered" in p for p in cov["problems"]), cov["problems"]


# ----------------------------------------------------------------- the closed loop (CLI)
class _ScriptedLLM:
    """Returns a coverage-broken draft first, then a fixed one — so we can prove the loop feeds the
    coverage gap back into a repair pass and converges."""
    name = "scripted"

    def __init__(self):
        self.calls = []

    def author(self, brief, schema, prior=None, errors=None):
        self.calls.append(errors)
        if prior is None:
            return _survey([_dq("screened", ["yes", "no"], operator="is_one_of"), _COMPLETE])  # broken
        return _survey([_dq("screened", "no"), _COMPLETE])                                      # fixed


def test_cli_author_repairs_against_coverage(tmp_path, monkeypatch, capsys):
    fake = _ScriptedLLM()
    monkeypatch.setattr(ai, "get_llm", lambda: fake)
    rc = cli.main(["author", "screen users", "--out", str(tmp_path),
                   "--simulate", "60", "--max-repairs", "2"])
    assert rc == 0
    # it took at least two drafts, and the 2nd draft was handed the coverage gap as feedback
    assert len(fake.calls) >= 2
    assert fake.calls[1] is not None and any("[coverage]" in e for e in fake.calls[1])
    out = capsys.readouterr().out
    assert "[coverage]" in out and "gap(s)" in out and "coverage gate passed" in out
    spec = json.loads(next(tmp_path.glob("*.survey.json")).read_text())
    assert simulate.coverage(spec, n=60)["problems"] == []


def test_cli_author_gives_up_and_flags_when_unfixable(tmp_path, monkeypatch, capsys):
    # an LLM that keeps returning the SAME broken draft: the loop must stop (no-progress guard)
    # and report not-launch-ready rather than spin.
    broken = _survey([_dq("screened", ["yes", "no"], operator="is_one_of"), _COMPLETE])
    monkeypatch.setattr(ai, "get_llm", lambda: type("L", (), {
        "name": "stuck", "author": lambda self, *a, **k: broken})())
    rc = cli.main(["author", "x", "--out", str(tmp_path), "--simulate", "40", "--max-repairs", "2"])
    assert rc == 1
    assert "not launch-ready" in capsys.readouterr().out


# ----------------------------------------------------------------- incidence targeting
def test_parse_target_incidence():
    f = cli._parse_target_incidence
    assert f("screen streamers, incidence 40%") == 40
    assert f("expect ~30% to qualify") == 30
    assert f("aim for 25% incidence, quota 300") == 25
    assert f("screen US adults who stream") is None     # no number → no target
    assert f("route 500 qualifiers to a task") is None  # 500 is not a valid incidence
    assert f("") is None


class _IncidenceLLM(ai.StubLLM):
    """Coverage-clean survey (qualifies unless q_use == 'no'), but every PERSONA answers q_use the
    same way — so the persona incidence is fully controlled while random coverage still passes."""
    def __init__(self, persona_answer):
        self._pa = persona_answer

    def author(self, brief, schema, prior=None, errors=None):
        return _survey([_dq("non_user", "no"), _COMPLETE])

    def respond(self, persona, question):
        return self._pa if question.get("id") == "q_use" else super().respond(persona, question)


def test_author_incidence_gate_flags_overscreened(tmp_path, monkeypatch, capsys):
    # personas all answer 'no' → 0% qualify, but target 40% → an [incidence] gap must be raised
    # (and random coverage still passes, proving the two gates are independent).
    monkeypatch.setattr(ai, "get_llm", lambda: _IncidenceLLM("no"))
    rc = cli.main(["author", "x", "--out", str(tmp_path), "--simulate", "60",
                   "--target-incidence", "40", "--max-repairs", "0"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[incidence]" in out and "0%" in out


def test_author_no_target_skips_incidence_gate(tmp_path, monkeypatch, capsys):
    # same coverage-clean survey, but with NO target the incidence gate never runs → it passes.
    monkeypatch.setattr(ai, "get_llm", lambda: _IncidenceLLM("no"))
    rc = cli.main(["author", "x", "--out", str(tmp_path), "--simulate", "60", "--max-repairs", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "coverage gate passed" in out and "[incidence]" not in out


# ----------------------------------------------------------------- LOI (length of interview)
def test_parse_target_loi():
    f = cli._parse_target_loi
    assert f("a 3 min screener") == 3
    assert f("keep it under 5 minutes") == 5
    assert f("3-minute survey, incidence 40%") == 3
    assert f("LOI 4, quota 300") == 4
    assert f("quota 300 minimum") is None      # 'minimum' must NOT match 'min'
    assert f("screen US adults") is None


def _long_survey():
    # coverage-clean (consent yes/no) but the qualified path is long: 4 long-text questions.
    pages = [{"id": "p1", "questions": [
        {"id": "q_use", "type": "radio", "required": True, "title": "Consent?",
         "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]}]
    for i in range(4):
        pages.append({"id": f"p_open{i}", "questions": [
            {"id": f"q_open{i}", "type": "textarea", "required": True, "title": f"Tell us more ({i})"}]})
    return {"survey": {"id": "long", "version": 1, "title": "Long one",
                       "settings": {"url_variables": ["uid", "rid"], "theme": "paper"},
                       "pages": pages,
                       "actions": [_dq("no_consent", "no"), _COMPLETE]}}


def test_coverage_reports_loi():
    cov = simulate.coverage(_long_survey(), n=40)
    assert cov["loi_seconds"] and cov["loi_seconds"] > 120     # 4×textarea(35s)+consent ≈ 147s


def test_author_loi_gate_flags_too_long(tmp_path, monkeypatch, capsys):
    long_spec = _long_survey()
    monkeypatch.setattr(ai, "get_llm", lambda: type("L", (), {
        "name": "stuck", "author": lambda self, *a, **k: long_spec})())
    rc = cli.main(["author", "x", "--out", str(tmp_path), "--simulate", "40",
                   "--target-loi", "1", "--max-repairs", "0"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[loi]" in out and "min" in out
