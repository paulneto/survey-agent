"""`file` question type: engine validation of the answer reference, the upload-store seam
(local + s3-with-fake-client), and the stdlib multipart parser in the server."""
import io
import os

from survey_agent.engine.runtime import SurveyRuntime
from survey_agent.store.uploads import LocalUploadStore, S3UploadStore, get_upload_store


SURVEY = {"survey": {
    "id": "f", "title": "File", "settings": {"url_variables": []},
    "pages": [{"id": "p1", "questions": [
        {"id": "q_zip", "type": "file", "required": True,
         "validation": {"extensions": ["zip", "json"], "max_size_kb": 1024, "max_files": 1}},
    ]}],
    "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
}}

REF_OK = {"backend": "local", "key": "f/r/q_zip/insta.zip", "filename": "insta.zip",
          "size": 500 * 1024, "content_type": "application/zip"}


def _session(rt, answers):
    s = rt.new_session("f:1:1", {})
    rt.start(s, rt.initial_quota_state())
    return rt.validate_answers(rt.pages[0], answers, s)


def test_file_required_missing():
    rt = SurveyRuntime(SURVEY)
    errs = _session(rt, {})
    assert "q_zip" in errs and "required" in errs["q_zip"].lower()


def test_file_accepts_allowed_ext_and_size():
    rt = SurveyRuntime(SURVEY)
    assert _session(rt, {"q_zip": REF_OK}) == {}


def test_file_rejects_bad_extension():
    rt = SurveyRuntime(SURVEY)
    errs = _session(rt, {"q_zip": {**REF_OK, "filename": "insta.png"}})
    assert "q_zip" in errs and "not allowed" in errs["q_zip"]


def test_file_rejects_oversize():
    rt = SurveyRuntime(SURVEY)
    errs = _session(rt, {"q_zip": {**REF_OK, "size": 5 * 1024 * 1024}})
    assert "q_zip" in errs and "too large" in errs["q_zip"]


def test_file_too_many_files():
    rt = SurveyRuntime(SURVEY)
    errs = _session(rt, {"q_zip": [REF_OK, REF_OK]})   # max_files=1
    assert "q_zip" in errs and "at most 1" in errs["q_zip"]


def test_file_submit_stores_reference_and_completes():
    rt = SurveyRuntime(SURVEY)
    qs = rt.initial_quota_state()
    s = rt.new_session("f:1:2", {})
    rt.start(s, qs)
    step = rt.submit(s, "p1", {"q_zip": REF_OK}, qs)
    assert step["ok"] and step["terminal"]["status"] == "completed"
    assert s["answers"]["q_zip"] == REF_OK              # the reference is what persists (never bytes)


def test_file_question_validates_against_schema():
    from survey_agent.engine import validate
    assert not [e for e in validate.validate(SURVEY) if "schema" in e.lower()]


# ---- upload store: local ----

def test_local_upload_roundtrip(tmp_path):
    us = LocalUploadStore(root=str(tmp_path))
    ref = us.put("surv", "resp/1", "q_zip", "My File.zip", b"PK\x03\x04payload", content_type="application/zip")
    assert ref["backend"] == "local"
    assert ref["filename"] == "My_File.zip"             # sanitized
    assert ref["size"] == len(b"PK\x03\x04payload")
    assert "resp_1" in ref["key"]                       # path separators in ids are sanitized
    assert us.open(ref) == b"PK\x03\x04payload"


def test_local_upload_base_url(tmp_path):
    us = LocalUploadStore(root=str(tmp_path))
    us.base_url = "https://cdn.example/uploads"
    ref = us.put("s", "r", "q", "a.zip", b"x")
    assert ref["url"].startswith("https://cdn.example/uploads/")
    assert us.url(ref) == ref["url"]


# ---- upload store: s3 (fake client, no network / no boto3 needed) ----

class _FakeS3:
    def __init__(self):
        self.objs = {}
    def put_object(self, Bucket, Key, Body, **kw):
        self.objs[(Bucket, Key)] = Body
    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}
    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?sig=abc"


def test_s3_upload_roundtrip_and_presigned_url():
    us = S3UploadStore(bucket="b", client=_FakeS3())
    ref = us.put("surv", "resp", "q_zip", "insta.zip", b"zipbytes", content_type="application/zip")
    key = ref.pop("key")
    assert ref == {"backend": "s3", "bucket": "b",
                   "filename": "insta.zip", "size": 8, "content_type": "application/zip"}
    assert key.startswith("surv/resp/q_zip/") and key.endswith("_insta.zip")   # unique per upload
    ref["key"] = key
    assert us.open(ref) == b"zipbytes"
    assert us.url(ref).startswith(f"https://s3.example/b/{key}")


def test_s3_requires_bucket(monkeypatch):
    monkeypatch.delenv("SURVEY_UPLOAD_BUCKET", raising=False)
    import pytest
    with pytest.raises(ValueError):
        S3UploadStore()


def test_upload_store_factory(monkeypatch):
    monkeypatch.setenv("SURVEY_UPLOAD_STORE", "local")
    assert isinstance(get_upload_store(), LocalUploadStore)
    monkeypatch.setenv("SURVEY_UPLOAD_STORE", "nope")
    import pytest
    with pytest.raises(ValueError):
        get_upload_store()


# ---- server: multipart parsing (stdlib, no cgi) ----

def test_parse_post_multipart_fields_and_file():
    from survey_agent.server import parse_post
    boundary = "----testboundary"
    body = b"".join([
        b"--" + boundary.encode() + b"\r\n",
        b'Content-Disposition: form-data; name="session_id"\r\n\r\nS1\r\n',
        b"--" + boundary.encode() + b"\r\n",
        b'Content-Disposition: form-data; name="page_id"\r\n\r\np1\r\n',
        b"--" + boundary.encode() + b"\r\n",
        b'Content-Disposition: form-data; name="q_zip"; filename="insta.zip"\r\n',
        b"Content-Type: application/zip\r\n\r\n",
        b"PK\x03\x04binarybytes\r\n",
        b"--" + boundary.encode() + b"--\r\n",
    ])
    fields, files = parse_post({"Content-Type": f"multipart/form-data; boundary={boundary}"}, body)
    assert fields["session_id"] == ["S1"] and fields["page_id"] == ["p1"]
    assert "q_zip" not in fields                         # the file part is not a text field
    fn, ct, data = files["q_zip"][0]
    assert fn == "insta.zip" and ct == "application/zip" and data == b"PK\x03\x04binarybytes"


def test_parse_post_urlencoded_unchanged():
    from survey_agent.server import parse_post
    fields, files = parse_post({"Content-Type": "application/x-www-form-urlencoded"},
                               b"session_id=S1&page_id=p1&q1=yes")
    assert fields == {"session_id": ["S1"], "page_id": ["p1"], "q1": ["yes"]} and files == {}
