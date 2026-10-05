"""The Mailchimp reference integration — a worked example of the completion-integration seam.
No network: we mock the single _request method and assert the API calls it would make."""
import hashlib

from survey_agent import integrations
from survey_agent.integrations.mailchimp import MailchimpIntegration


def _session(answers=None, url_vars=None, variables=None):
    return {"survey_id": "streaming", "session_id": "s1",
            "answers": answers or {}, "url_vars": url_vars or {}, "variables": variables or {}}


def _mc(**kw):
    mc = MailchimpIntegration(api_key="key-us21", list_id="LST", **kw)
    mc.calls = []
    mc._request = lambda method, url, body: mc.calls.append((method, url, body))   # capture, no network
    return mc


def test_selectable_by_name():
    assert isinstance(integrations.get_integration("mailchimp"), MailchimpIntegration)


def test_upsert_and_tags_on_complete():
    mc = _mc(email_var="email", merge_map={"AGE": "q:q_age", "SEG": "var:user_segment"})
    sess = _session(answers={"q_age": 34}, url_vars={"email": "Sam@Example.com"},
                    variables={"user_segment": "active"})
    mc.terminal(sess, "completed")
    assert len(mc.calls) == 2
    (m1, url1, body1), (m2, url2, body2) = mc.calls
    # idempotent member upsert, keyed by the lowercased-email md5
    sub = hashlib.md5("sam@example.com".encode()).hexdigest()
    assert m1 == "PUT" and url1.endswith(f"/lists/LST/members/{sub}")
    assert url1.startswith("https://us21.api.mailchimp.com/3.0/")    # datacenter parsed from key
    assert body1["email_address"] == "Sam@Example.com"
    assert body1["merge_fields"] == {"AGE": 34, "SEG": "active"}
    # tags include the outcome (so a Customer Journey can trigger on it)
    assert m2 == "POST" and url2.endswith("/tags")
    names = {t["name"] for t in body2["tags"]}
    assert "outcome:completed" in names and "survey:streaming" in names


def test_disqualified_is_skipped_by_default_but_tagged_when_on_always():
    sess = _session(url_vars={"email": "a@b.com"})
    skip = _mc(); skip.terminal(sess, "disqualified", reason_code="non_user")
    assert skip.calls == []                                          # default on="completed"
    always = _mc(on="always"); always.terminal(sess, "disqualified", reason_code="non_user")
    names = {t["name"] for t in always.calls[1][2]["tags"]}
    assert "outcome:disqualified" in names and "reason:non_user" in names


def test_noop_without_email_or_config():
    mc = _mc(); mc.terminal(_session(), "completed")                 # no email anywhere
    assert mc.calls == []
    unconfigured = MailchimpIntegration(api_key="", list_id="")
    unconfigured._request = lambda *a: (_ for _ in ()).throw(AssertionError("should not call"))
    unconfigured.terminal(_session(url_vars={"email": "a@b.com"}), "completed")   # no api key -> no-op
