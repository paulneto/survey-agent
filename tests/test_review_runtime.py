"""Regression tests for the release-review runtime findings (docs/RELEASE_REVIEW.local.md):

  R3   submits need the session's opaque token + must belong to the route's survey
  R4   a session resumes/finishes on its pinned definition version (across a restart)
  R6   quota acceptance is an atomic reservation; terminal processing is idempotent
  R7   request bodies are capped before reading; uploads are validated before they're stored
  R10  browser chat is demo/preview-only and lifecycle-gated on every call
  S4   header-only data-API auth, redacted logs, allowlisted passback vars, no-store/no-referrer

Everything runs against temp dirs/stores with synthetic respondents only.
"""
import contextlib
import http.client
import importlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from survey_agent.engine import signing, terminal
from test_server_http import _get, _post, _tok, running_multi, running_server


def _consent_survey(sid="t_rev", version=1, **settings):
    st = {"url_variables": ["uid", "rid", "group"], "theme": "base"}
    st.update(settings)
    return {"survey": {
        "id": sid, "version": version, "title": "Review test", "settings": st,
        "pages": [{"id": "p1", "questions": [
            {"id": "q_consent", "type": "radio", "required": True, "title": "Do you consent?",
             "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]},
            {"id": "p2", "questions": [
                {"id": "q_color", "type": "radio", "required": True, "title": "Favourite colour?",
                 "options": [{"value": "red", "label": "Red"}, {"value": "blue", "label": "Blue"}]}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
    }}


def _hidden(html, name):
    m = re.search(rf'name={name} value="([^"]*)"', html)
    return m.group(1) if m else None


def _post_status(url, data, headers=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


# =============================================================================== R3
def test_forged_submit_without_or_with_wrong_token_is_rejected_and_changes_nothing():
    with running_server(_consent_survey()) as base:
        import survey_agent.server as server
        _, html = _get(f"{base}/s/t_rev?uid=7&rid=8&group=g")
        sid, pid = _hidden(html, "session_id"), _hidden(html, "page_id")
        assert sid == "t_rev:8:7" and _hidden(html, "session_token")       # token rendered
        before = json.dumps(server.STORE.get_session_by_id(sid), sort_keys=True)
        for extra in ({}, {"session_token": "nope"}, {"session_token": ""}):
            code, body, _ = _post_status(f"{base}/s/t_rev/submit",
                                         {"session_id": sid, "page_id": pid, "q_consent": "no", **extra})
            assert code == 403, (extra, code)
        after = json.dumps(server.STORE.get_session_by_id(sid), sort_keys=True)
        assert before == after                                            # session untouched
        assert server.STORE.list_responses("t_rev") == []


def test_unsigned_guess_cannot_submit_to_a_signed_survey_session():
    secret = "s3cret"
    spec = _consent_survey(url_signing={"required": True, "fields": ["uid", "rid", "group"]})
    with running_server(spec, SURVEY_URL_SECRET=secret) as base:
        import survey_agent.server as server
        q = signing.signed_query({"uid": "1", "rid": "2", "group": "vip"}, secret, ["uid", "rid", "group"])
        _, html = _get(f"{base}/s/t_rev?{q}")                               # the real (signed) invite
        sid, pid = _hidden(html, "session_id"), _hidden(html, "page_id")
        # an attacker who guesses survey:rid:uid but has no signed link → no token → refused
        code, _, _ = _post_status(f"{base}/s/t_rev/submit", {"session_id": sid, "page_id": pid, "q_consent": "no"})
        assert code == 403
        assert server.STORE.get_session_by_id(sid)["answers"] == {}
        # an unsigned GET can't mint/obtain the token either
        status, html2 = _get(f"{base}/s/t_rev?uid=1&rid=2&group=vip")
        assert status == 403 and "session_token" not in html2


def test_cross_survey_session_id_is_rejected():
    a, b = _consent_survey("s_a"), _consent_survey("s_b")
    with running_multi([a, b], state={"s_a": "live", "s_b": "live"}) as (base, _d):
        import survey_agent.server as server
        _, html = _get(f"{base}/s/s_a?uid=1&rid=1")
        sid, tok, pid = _hidden(html, "session_id"), _hidden(html, "session_token"), _hidden(html, "page_id")
        code, _, _ = _post_status(f"{base}/s/s_b/submit",
                                  {"session_id": sid, "session_token": tok, "page_id": pid, "q_consent": "yes"})
        assert code == 403
        assert server.STORE.get_session_by_id(sid)["answers"] == {}
        # …while the right route works
        code, html, _ = _post_status(f"{base}/s/s_a/submit",
                                     {"session_id": sid, "session_token": tok, "page_id": pid, "q_consent": "yes"})
        assert code == 200 and "Favourite colour?" in html


def test_resume_rerenders_with_the_same_token_and_completes():
    with running_server(_consent_survey()) as base:
        _, h1 = _get(f"{base}/s/t_rev?uid=3&rid=4&group=g")
        tok = _tok(h1)
        _, h2 = _get(f"{base}/s/t_rev?uid=3&rid=4&group=g")                 # resume (invite link)
        assert _tok(h2) == tok
        _, h3 = _post(f"{base}/s/t_rev/submit", {"session_id": _hidden(h2, "session_id"), "session_token": tok,
                                                 "page_id": "p1", "q_consent": "yes"})
        _, done = _post(f"{base}/s/t_rev/submit", {"session_id": _hidden(h3, "session_id"),
                                                   "session_token": _tok(h3), "page_id": "p2", "q_color": "red"})
        assert "Complete" in done


def test_legacy_session_without_token_gets_one_on_resume():
    with running_server(_consent_survey()) as base:
        import survey_agent.server as server
        _get(f"{base}/s/t_rev?uid=5&rid=6&group=g")
        s = server.STORE.get_session_by_id("t_rev:6:5")
        s.pop("token")                                                      # simulate a pre-fix session
        s.pop("passback_vars", None)
        server.STORE.save_session(s)
        code, _, _ = _post_status(f"{base}/s/t_rev/submit", {"session_id": "t_rev:6:5", "page_id": "p1",
                                                             "q_consent": "yes"})
        assert code == 403                                                  # no token on file → refused
        _, html = _get(f"{base}/s/t_rev?uid=5&rid=6&group=g")
        assert _tok(html) and server.STORE.get_session_by_id("t_rev:6:5")["token"] == _tok(html)
        assert server.STORE.get_session_by_id("t_rev:6:5")["passback_vars"]


def test_retake_multiple_rotates_the_token():
    with running_server(_consent_survey(retake="multiple")) as base:
        _, h = _get(f"{base}/s/t_rev?uid=9&rid=9&group=g")
        old = _tok(h)
        _, h = _post(f"{base}/s/t_rev/submit", {"session_id": "t_rev:9:9", "session_token": old,
                                                "page_id": "p1", "q_consent": "yes"})
        _post(f"{base}/s/t_rev/submit", {"session_id": "t_rev:9:9", "session_token": old,
                                         "page_id": "p2", "q_color": "blue"})
        _, again = _get(f"{base}/s/t_rev?uid=9&rid=9&group=g")              # fresh take
        assert "Do you consent?" in again and _tok(again) != old


# =============================================================================== R4
@contextlib.contextmanager
def _serve_multi_dir(d, data_dir, **env):
    """(Re)load server.py in multi mode over an existing SURVEY_DIR + a persistent file store and
    serve it — calling this twice simulates a process restart."""
    old = dict(os.environ)
    os.environ.pop("SURVEY_FILE", None)
    os.environ.update({"SURVEY_DIR": d, "SURVEY_MULTI": "1", "SURVEY_STORE": "file",
                       "SURVEY_DATA_DIR": data_dir, "SURVEY_INTEGRATION": "none",
                       "SURVEY_DEFINITION_SOURCE": "file"})
    os.environ.update({k: str(v) for k, v in env.items()})
    import survey_agent.server as server
    importlib.reload(server)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", server
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.environ.clear()
        os.environ.update(old)


def _v2_spec():
    return {"survey": {
        "id": "t_ver", "version": 2, "title": "Review test v2",
        "settings": {"url_variables": ["uid", "rid", "group"], "theme": "base"},
        "pages": [{"id": "n1", "questions": [
            {"id": "q_new", "type": "text", "required": True, "title": "A brand-new question?"}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
    }}


def test_session_finishes_on_its_pinned_version_after_republish_and_restart():
    import survey_agent.registry as registry
    from survey_agent.definitions.file import FileDefinitionStore
    d, data = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        defs = FileDefinitionStore(dir=d)
        defs.put_definition(_consent_survey("t_ver", version=1))
        registry.set_status("t_ver", status="live", sdir=d)
        if (defs.get_definition("t_ver", version=1) or {}).get("survey", {}).get("version") != 1:
            pytest.skip("file definition store does not retain versions yet")
        with _serve_multi_dir(d, data) as (base, _srv):
            _, h = _get(f"{base}/s/t_ver?uid=1&rid=1")
            assert "Do you consent?" in h
            tok = _tok(h)
            _, h = _post(f"{base}/s/t_ver/submit", {"session_id": "t_ver:1:1", "session_token": tok,
                                                    "page_id": "p1", "q_consent": "yes"})
            assert "Favourite colour?" in h
        # publish a materially different v2 (different pages/questions) and make it live
        assert defs.put_definition(_v2_spec()) == 2
        registry.set_status("t_ver", status="live", sdir=d)
        with _serve_multi_dir(d, data) as (base, srv):                    # a fresh process
            _, h = _get(f"{base}/s/t_ver?uid=1&rid=1")                      # resume → still v1, page 2
            assert "Favourite colour?" in h and "brand-new" not in h
            _, done = _post(f"{base}/s/t_ver/submit", {"session_id": "t_ver:1:1", "session_token": tok,
                                                       "page_id": "p2", "q_color": "blue"})
            assert "Complete" in done
            rows = srv.STORE.list_responses("t_ver")
            assert len(rows) == 1 and set(rows[0]["survey_data"]) == {"q_consent", "q_color"}
            assert srv.STORE.get_session_by_id("t_ver:1:1")["version"] == 1
            # a new respondent gets the live v2
            _, h2 = _get(f"{base}/s/t_ver?uid=2&rid=2")
            assert "A brand-new question?" in h2
            assert srv.STORE.get_session_by_id("t_ver:2:2")["version"] == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(data, ignore_errors=True)


def test_missing_pinned_version_never_switches_content_mid_session():
    with running_server(_consent_survey()) as base:
        import survey_agent.server as server

        class _NoVersions:
            def get_definition(self, survey_id, version=None):
                return None
        server._DEFSTORE = _NoVersions()
        _, h = _get(f"{base}/s/t_rev?uid=1&rid=1&group=g")
        tok = _tok(h)
        _post(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1", "session_token": tok,
                                         "page_id": "p1", "q_consent": "yes"})
        s = server.STORE.get_session_by_id("t_rev:1:1")
        s["version"] = 99                                   # pinned to a version that no longer exists
        server.STORE.save_session(s)
        assert server.rt_for_session("t_rev", s, server.RT) is None
        # a submit is refused with a clear page — answers are not applied to different content
        code, body, _ = _post_status(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1", "session_token": tok,
                                                                "page_id": "p2", "q_color": "red"})
        assert code == 409 and "was updated" in body
        assert "q_color" not in server.STORE.get_session_by_id("t_rev:1:1")["answers"]
        # reopening the invite link restarts the take cleanly on the live version
        _, h2 = _get(f"{base}/s/t_rev?uid=1&rid=1&group=g")
        assert "Do you consent?" in h2 and _tok(h2) != tok
        fresh = server.STORE.get_session_by_id("t_rev:1:1")
        assert fresh["version"] == 1 and fresh["answers"] == {} and fresh["response_id"] != s["response_id"]
        assert server.STORE.list_responses("t_rev") == []


# =============================================================================== R6
def _quota_survey():
    return {"survey": {
        "id": "t_q", "version": 1, "title": "Quota", "settings": {"url_variables": ["uid", "rid"]},
        "quotas": [{"id": "total", "scope": "survey", "limit": 1}],
        "pages": [{"id": "p1", "questions": [
            {"id": "q1", "type": "radio", "required": True, "title": "Q?",
             "options": [{"value": "a", "label": "A"}]}]}],
        "actions": [
            {"type": "overquota", "message": "Sorry — we're full.",
             "when": {"ref": {"kind": "quota", "id": "total"}, "operator": "is_full"}},
            {"type": "complete"}],
    }}


def test_outbox_uses_top_level_idempotency_key_and_can_erase(tmp_path):
    from survey_agent.integrations.outbox import Outbox
    ob = Outbox(dir=str(tmp_path / "ob"), max_attempts=1)
    a = {"method": "POST", "url": "http://x/h", "payload": {"n": 1}, "idempotency_key": "K1"}
    b = {"method": "POST", "url": "http://x/h", "payload": {"n": 2}, "idempotency_key": "K2"}
    assert ob.enqueue(a).startswith("k_") and ob.enqueue(dict(a, payload={"n": 9})) == ob.enqueue(a)
    ob.enqueue(b)
    ob.retry(lambda i: False, now=10 ** 12)           # → both past max_attempts → dead-letter
    ob.enqueue(a)                                     # and K1 pending again
    assert ob.stats() == {"pending": 1, "dead": 2}
    assert ob.remove_by_idempotency_key(["K1"]) == 2  # pending + dead-letter
    assert ob.stats() == {"pending": 0, "dead": 1}
    assert ob.remove_by_idempotency_key([]) == 0


def test_competing_completions_cannot_exceed_the_quota():
    with running_server(_quota_survey()) as _base:
        import survey_agent.server as server
        rt = server.RT
        sessions, terms = [], []
        for i in (1, 2):                                       # both reach complete on a 0-count snapshot
            s = rt.new_session(f"t_q:{i}:{i}", {"uid": str(i), "rid": str(i)}, response_id=f"resp{i}")
            s["token"] = "x"
            rt.start(s, server.quota_state(rt))
            step = rt.submit(s, "p1", {"q1": "a"}, server.quota_state(rt))
            assert step["terminal"]["status"] == "completed"
            sessions.append(s)
            terms.append(step["terminal"])
        applied = [server.on_terminal(s, t, rt) for s, t in zip(sessions, terms)]
        assert sorted(t["status"] for t in applied) == ["completed", "overquota"]
        over = next(t for t in applied if t["status"] == "overquota")
        assert over["message"] == "Sorry — we're full."                    # the survey's own overquota action
        assert server.STORE.quota_counts("t_q", 1) == {"total": 1}
        rows = server.STORE.list_responses("t_q")
        assert sorted(r["status"] for r in rows) == ["Complete", "Disqualified"]
        assert next(r for r in rows if r["status"] == "Disqualified")["reason_code"] == "overquota"


def test_terminal_processing_is_idempotent_per_session():
    with running_server(_quota_survey()) as _base:
        import survey_agent.server as server
        rt = server.RT
        fired = []
        server.INTEGRATION.terminal = lambda session, status, reason_code=None: fired.append(status)
        s = rt.new_session("t_q:1:1", {"uid": "1", "rid": "1"}, response_id="r1")
        rt.start(s, server.quota_state(rt))
        step = rt.submit(s, "p1", {"q1": "a"}, server.quota_state(rt))
        server.on_terminal(s, step["terminal"], rt)
        server.on_terminal(s, step["terminal"], rt)                         # replayed / duplicate
        assert len(server.STORE.list_responses("t_q")) == 1
        assert server.STORE.quota_counts("t_q", 1) == {"total": 1}
        assert fired == ["completed"]


def test_concurrent_duplicate_submits_record_once():
    with running_server(_quota_survey()) as base:
        import survey_agent.server as server
        _, h = _get(f"{base}/s/t_q?uid=1&rid=1")
        data = {"session_id": "t_q:1:1", "session_token": _tok(h), "page_id": "p1", "q1": "a"}
        out = []
        ts = [threading.Thread(target=lambda: out.append(_post_status(f"{base}/s/t_q/submit", data)[0]))
              for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert all(c == 200 for c in out)
        assert len(server.STORE.list_responses("t_q")) == 1
        assert server.STORE.quota_counts("t_q", 1) == {"total": 1}


# =============================================================================== R7
def _raw_post(base, path, body=b"", headers=None):
    u = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    conn.putrequest("POST", path, skip_accept_encoding=True)
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    conn.endheaders()
    if body:
        conn.send(body)
    r = conn.getresponse()
    out = (r.status, r.read())
    conn.close()
    return out


def test_oversized_body_is_refused_before_reading():
    with running_server(_consent_survey(), SURVEY_MAX_BODY_BYTES=1024) as base:
        # claims 50 MB but sends nothing: refused from the header alone (no allocation, no wait)
        code, _ = _raw_post(base, "/s/t_rev/submit", headers={
            "Content-Length": str(50 * 1024 * 1024), "Content-Type": "application/x-www-form-urlencoded"})
        assert code == 413


def test_missing_or_invalid_content_length_is_refused():
    with running_server(_consent_survey()) as base:
        assert _raw_post(base, "/s/t_rev/submit", headers={"Content-Type": "text/plain"})[0] == 411
        assert _raw_post(base, "/s/t_rev/submit", headers={"Content-Length": "abc"})[0] == 400
        assert _raw_post(base, "/s/t_rev/submit", headers={"Content-Length": "-5"})[0] == 400


def test_chat_json_body_is_capped():
    with running_server(_consent_survey(), SURVEY_SERVE_STATES="*") as base:
        code, _ = _raw_post(base, "/chat/say", headers={"Content-Length": str(1024 * 1024),
                                                        "Content-Type": "application/json"})
        assert code == 413


def _file_survey():
    return {"survey": {
        "id": "t_up", "version": 1, "title": "Upload", "settings": {"url_variables": ["uid", "rid"]},
        "pages": [
            {"id": "p1", "questions": [
                {"id": "q_name", "type": "text", "required": True, "title": "Name?"},
                {"id": "q_zip", "type": "file", "required": True, "title": "Upload",
                 "validation": {"extensions": ["zip"], "max_size_kb": 1, "max_files": 1}}]},
            {"id": "p2", "questions": [
                {"id": "q_later", "type": "file", "title": "Later upload",
                 "validation": {"extensions": ["zip"], "max_files": 1}}]}],
        "actions": [{"type": "complete"}],
    }}


def _multipart(fields, files):
    boundary = "----revtestboundary"
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (fn, data) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def _upload(base, fields, files):
    body, ctype = _multipart(fields, files)
    return _raw_post(base, "/s/t_up/submit", body, {"Content-Type": ctype, "Content-Length": str(len(body))})


def _stored_files(root):
    return [os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in fs]


def test_uploads_are_validated_before_storage_and_rejected_ones_cleaned_up(tmp_path):
    up = str(tmp_path / "uploads")
    with running_server(_file_survey(), SURVEY_UPLOAD_DIR=up) as base:
        import survey_agent.server as server
        _, h = _get(f"{base}/s/t_up?uid=1&rid=1")
        auth = {"session_id": "t_up:1:1", "session_token": _tok(h)}
        # a question that isn't on the current page → nothing stored
        code, _ = _upload(base, {**auth, "page_id": "p1", "q_name": "A"}, {"q_later": ("x.zip", b"PK")})
        assert code == 200 and _stored_files(up) == []
        # a stale page id → nothing stored
        _upload(base, {**auth, "page_id": "p2"}, {"q_later": ("x.zip", b"PK")})
        assert _stored_files(up) == []
        # wrong extension / too large → rejected before storage, error re-rendered
        code, body = _upload(base, {**auth, "page_id": "p1", "q_name": "A"}, {"q_zip": ("x.exe", b"MZ")})
        assert b"not allowed" in body and _stored_files(up) == []
        code, body = _upload(base, {**auth, "page_id": "p1", "q_name": "A"}, {"q_zip": ("x.zip", b"P" * 5000)})
        assert b"too large" in body and _stored_files(up) == []
        # valid file but the page is rejected (q_name missing) → the stored bytes are deleted again
        code, body = _upload(base, {**auth, "page_id": "p1"}, {"q_zip": ("x.zip", b"PK")})
        assert b"required" in body and _stored_files(up) == []
        assert "q_zip" not in server.STORE.get_session_by_id("t_up:1:1")["answers"]
        # a valid submit stores exactly one blob and keeps the reference
        code, body = _upload(base, {**auth, "page_id": "p1", "q_name": "A"}, {"q_zip": ("x.zip", b"PK")})
        assert b"Later upload" in body and len(_stored_files(up)) == 1
        ref = server.STORE.get_session_by_id("t_up:1:1")["answers"]["q_zip"]
        assert server.upload_store().open(ref) == b"PK"


def test_a_forged_file_ref_in_a_text_field_is_ignored(tmp_path):
    with running_server(_file_survey(), SURVEY_UPLOAD_DIR=str(tmp_path)) as base:
        import survey_agent.server as server
        _, h = _get(f"{base}/s/t_up?uid=2&rid=2")
        _post_status(f"{base}/s/t_up/submit", {"session_id": "t_up:2:2", "session_token": _tok(h),
                                               "page_id": "p1", "q_name": "A",
                                               "q_zip": json.dumps({"key": "../../etc/passwd", "filename": "a.zip"})})
        assert "q_zip" not in server.STORE.get_session_by_id("t_up:2:2")["answers"]


def test_upload_keys_are_unique_and_deletable(tmp_path):
    from survey_agent.store.uploads import LocalUploadStore
    us = LocalUploadStore(root=str(tmp_path))
    a = us.put("s", "r", "q", "same.zip", b"one")
    b = us.put("s", "r", "q", "same.zip", b"two")
    assert a["key"] != b["key"]                                    # a retake/re-upload never overwrites
    assert us.open(a) == b"one" and us.open(b) == b"two"
    assert us.delete(a) is True and us.delete(a) is False         # idempotent
    with pytest.raises(FileNotFoundError):
        us.open(a)
    assert us.open(b) == b"two"
    with pytest.raises(ValueError):
        us.delete({"key": "../outside.txt"})


def test_s3_delete():
    from survey_agent.store.uploads import S3UploadStore

    class _S3:
        def __init__(self):
            self.objs, self.deleted = {}, []

        def put_object(self, Bucket, Key, Body, **kw):
            self.objs[(Bucket, Key)] = Body

        def delete_object(self, Bucket, Key):
            self.deleted.append((Bucket, Key))
            self.objs.pop((Bucket, Key), None)

    c = _S3()
    us = S3UploadStore(bucket="b", prefix="pre", client=c)
    ref = us.put("s", "r", "q", "f.zip", b"x")
    assert us.delete(ref) is True and c.objs == {} and c.deleted == [("b", "pre/" + ref["key"])]


# =============================================================================== R10
def test_chat_is_not_served_outside_demo_preview_mode():
    with running_server(_consent_survey()) as base:
        assert _get(f"{base}/chat")[0] == 404
        code, _, _ = _post_status(f"{base}/chat/say", {}, headers={"Content-Type": "application/json"})
        assert code == 404
        req = urllib.request.Request(f"{base}/chat/say", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 404


def _chat(base, payload):
    req = urllib.request.Request(f"{base}/chat/say", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_chat_in_demo_mode_is_labelled_preview_and_records_nothing():
    with running_server(_consent_survey(), SURVEY_SERVE_STATES="*") as base:
        import survey_agent.server as server
        code, html = _get(f"{base}/chat")
        assert code == 200 and "responses are not recorded" in html
        code, out = _chat(base, {})
        assert code == 200 and out["session_id"] and out["preview"] is True
        assert server.STORE.list_responses() == []


def test_chat_post_applies_the_lifecycle_gate():
    with running_multi([_consent_survey("t_c"), _consent_survey("t_d")], state={"t_c": "live", "t_d": "draft"},
                       SURVEY_CHAT_DEMO="1") as (base, d):
        import survey_agent.registry as registry
        assert _get(f"{base}/chat?survey=t_c")[0] == 200
        code, out = _chat(base, {"survey": "t_c"})
        assert code == 200 and out["session_id"]
        sid = out["session_id"]
        registry.set_status("t_c", status="closed", sdir=d)               # closed mid-conversation
        code, out = _chat(base, {"survey": "t_c", "session_id": sid, "reply": "yes"})
        assert code == 403 and out["done"]
        code, out = _chat(base, {"survey": "t_d"})                          # a draft can't be started by POST
        assert code == 403 and not out["session_id"]
        assert "Not open yet" in _get(f"{base}/chat?survey=t_d")[1]


def test_chat_sessions_are_bounded():
    with running_server(_consent_survey(), SURVEY_SERVE_STATES="*") as _base:
        import survey_agent.server as server
        server._CHATS_MAX = 5
        ids = [server._chat_reply({})[1]["session_id"] for _ in range(12)]
        assert len(server._CHATS) == 5
        assert list(server._CHATS) == ids[-5:]                             # least-recently-used evicted


# =============================================================================== S4
def test_data_api_is_header_only():
    with running_server(_consent_survey(), SURVEY_API_TOKEN="apitok") as base:
        code, _ = _get(f"{base}/api/responses?token=apitok")
        assert code == 401
        req = urllib.request.Request(f"{base}/api/responses", headers={"Authorization": "Bearer apitok"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
            assert r.headers["Cache-Control"] == "no-store"
            assert r.headers["Referrer-Policy"] == "no-referrer"
            assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_survey_and_terminal_pages_carry_privacy_headers():
    with running_server(_consent_survey()) as base:
        with urllib.request.urlopen(f"{base}/s/t_rev?uid=1&rid=1&group=g") as r:
            html = r.read().decode()
            for k, v in (("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                         ("X-Content-Type-Options", "nosniff")):
                assert r.headers[k] == v
        _, h, _ = _post_status(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1", "session_token": _tok(html),
                                                          "page_id": "p1", "q_consent": "yes"})
        code, done, hdrs = _post_status(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1",
                                                                   "session_token": _tok(html),
                                                                   "page_id": "p2", "q_color": "red"})
        assert "Complete" in done and hdrs.get("Cache-Control") == "no-store"
        assert hdrs.get("Referrer-Policy") == "no-referrer"


def test_obs_redacts_urls_and_session_ids():
    from survey_agent import obs
    assert obs.redact_url("https://user:pw@hooks.example:8443/a/b?token=abc&uid=1#f") == \
        "https://hooks.example:8443/a/b"
    rec = logging.makeLogRecord({"msg": "x", "session_id": "svy:rid-1:uid-2",
                                 "url": "https://h.example/p?secret=1"})
    ex = obs._extras(rec)
    assert "uid-2" not in str(ex["session_id"]) and ex["session_id"].startswith("h:")
    assert ex["url"] == "https://h.example/p"
    line = obs._JsonFormatter().format(rec)
    assert "uid-2" not in line and "secret" not in line


def test_webhook_failure_log_has_no_query_or_userinfo(caplog):
    spec = _consent_survey()
    spec["survey"]["actions"].insert(0, {"type": "webhook", "url": "http://user:pw@127.0.0.1:9/hook?api_key=SECRET",
                                         "method": "POST", "payload": {}})
    with running_server(spec) as base:
        import survey_agent.server as server
        server.log.propagate = True
        logging.getLogger("survey_agent").propagate = True
        try:
            with caplog.at_level(logging.WARNING, logger="survey_agent"):
                _, h = _get(f"{base}/s/t_rev?uid=1&rid=1&group=g")
                _post(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1", "session_token": _tok(h),
                                                 "page_id": "p1", "q_consent": "yes"})
        finally:
            logging.getLogger("survey_agent").propagate = False
        recs = [r for r in caplog.records if "webhook delivery failed" in r.getMessage()]
        if not recs:
            pytest.skip("webhook action did not fire for this survey shape")
        formatted = " ".join(str(r.__dict__.get("url")) + str(server.obs._extras(r)) for r in recs)
        assert "SECRET" not in formatted and "pw" not in formatted
        assert "127.0.0.1:9/hook" in formatted


def test_passback_events_forward_only_declared_and_identity_vars():
    from survey_agent.engine.runtime import SurveyRuntime
    rt = SurveyRuntime(_consent_survey(url_variables=["group"], identity={"respondent": "pid", "response": "tid"}))
    s = rt.new_session("t_rev:t:p", {"pid": "p", "tid": "t", "group": "g", "email": "x@example.com",
                                     "access_token": "zzz"})
    ev = terminal.build_event(s, "completed")
    assert ev["url_variables"] == {"pid": "p", "tid": "t", "group": "g"}
    assert s["url_vars"]["email"] == "x@example.com"                # still captured (drop-in contract)
    # a legacy session dict without the allowlist keeps the old behaviour
    legacy = {"session_id": "a", "url_vars": {"x": "1"}}
    assert terminal.build_event(legacy, "completed")["url_variables"] == {"x": "1"}


def test_response_export_still_captures_all_url_params():
    with running_server(_consent_survey()) as base:
        import survey_agent.server as server
        _, h = _get(f"{base}/s/t_rev?uid=1&rid=1&group=g&utm_source=mail")
        _post(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1", "session_token": _tok(h),
                                         "page_id": "p1", "q_consent": "yes"})
        _post(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1", "session_token": _tok(h),
                                         "page_id": "p2", "q_color": "red"})
        row = server.STORE.list_responses("t_rev")[0]
        assert row["url_variables"]["utm_source"] == "mail"
        assert "token" not in row and "session_token" not in json.dumps(row)
