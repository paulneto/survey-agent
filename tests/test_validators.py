from survey_agent.engine.runtime import SurveyRuntime


def _rt(questions):
    return SurveyRuntime({"survey": {"id": "v", "title": "V", "pages": [{"id": "p", "questions": questions}],
                                     "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}})


def _errs(rt, answers):
    return rt.validate_answers(rt.pages[0], answers, rt.new_session("v:1", {}))


def test_email_format():
    rt = _rt([{"id": "q", "type": "text", "validation": {"format": "email"}}])
    assert "q" in _errs(rt, {"q": "not-an-email"})
    assert "q" not in _errs(rt, {"q": "a@b.com"})


def test_phone_format_intl_default_accepts_us_uk_and_e164():
    rt = _rt([{"id": "q", "type": "text", "validation": {"format": "phone"}}])
    for good in ("(415) 555-0132", "07700 900123", "+44 7700 900123", "+33 6 12 34 56 78"):
        assert "q" not in _errs(rt, {"q": good}), good
    for bad in ("12345", "hello", "abcd-efgh"):
        assert "q" in _errs(rt, {"q": bad}), bad


def test_phone_format_region_constrains():
    us = _rt([{"id": "q", "type": "text", "validation": {"format": "phone", "phone_region": "us"}}])
    assert "q" not in _errs(us, {"q": "415-555-0132"})
    assert "q" in _errs(us, {"q": "+33 6 12 34 56 78"})        # a French number is rejected for US-only
    uk = _rt([{"id": "q", "type": "text", "validation": {"format": "phone", "phone_region": "uk"}}])
    assert "q" not in _errs(uk, {"q": "020 7946 0958"})


def test_percent_range():
    rt = _rt([{"id": "q", "type": "number", "validation": {"percent": True}}])
    assert "q" in _errs(rt, {"q": 150})
    assert "q" in _errs(rt, {"q": -1})
    assert "q" not in _errs(rt, {"q": 50})


def test_text_length():
    rt = _rt([{"id": "q", "type": "textarea", "validation": {"min_length": 5, "max_length": 10}}])
    assert "q" in _errs(rt, {"q": "hi"})
    assert "q" in _errs(rt, {"q": "x" * 20})
    assert "q" not in _errs(rt, {"q": "hello"})


def test_min_words():
    rt = _rt([{"id": "q", "type": "textarea", "validation": {"min_words": 3}}])
    assert "q" in _errs(rt, {"q": "asdf"})                 # one token → blocked
    assert "q" in _errs(rt, {"q": "two words"})            # below the floor
    assert "q" not in _errs(rt, {"q": "three whole words"})


def test_date_format_and_bounds():
    rt = _rt([{"id": "q", "type": "date", "validation": {"min": "2020-01-01", "max": "2030-01-01"}}])
    assert "q" in _errs(rt, {"q": "not-a-date"})
    assert "q" in _errs(rt, {"q": "2010-05-05"})        # before min
    assert "q" in _errs(rt, {"q": "2031-01-01"})        # after max
    assert "q" not in _errs(rt, {"q": "2025-06-01"})


def test_number_min_max_still_works():
    rt = _rt([{"id": "q", "type": "number", "validation": {"min": 18, "max": 99}}])
    assert "q" in _errs(rt, {"q": 5})
    assert "q" not in _errs(rt, {"q": 40})
