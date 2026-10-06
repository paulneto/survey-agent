"""HMAC URL-param signing — the pure primitive behind "signable URL params". Verifies the round-trip,
tamper detection, that it's blind to unsigned params + query order, and that a missing/bad sig fails
closed."""
from survey_agent.engine import signing

SECRET = "shared-secret-123"
FIELDS = ["uid", "rid", "group"]


def test_sign_verify_roundtrip():
    p = {"uid": "u1", "rid": "r1", "group": "vip"}
    signed = signing.sign_params(p, SECRET, FIELDS)
    assert "sig" in signed
    assert signing.verify(signed, SECRET, FIELDS)


def test_tampering_with_a_signed_field_is_detected():
    signed = signing.sign_params({"uid": "u1", "rid": "r1", "group": "free"}, SECRET, FIELDS)
    forged = dict(signed, group="vip")            # respondent upgrades themselves to the vip cell
    assert not signing.verify(forged, SECRET, FIELDS)


def test_wrong_secret_fails():
    signed = signing.sign_params({"uid": "u1", "rid": "r1", "group": "vip"}, SECRET, FIELDS)
    assert not signing.verify(signed, "different-secret", FIELDS)


def test_missing_signature_fails_closed():
    assert not signing.verify({"uid": "u1", "rid": "r1", "group": "vip"}, SECRET, FIELDS)
    assert not signing.verify({"uid": "u1", "sig": ""}, SECRET, FIELDS)
    assert not signing.verify({"uid": "u1", "sig": None}, SECRET, FIELDS)   # never raises


def test_signature_is_blind_to_unsigned_params_and_order():
    base = {"uid": "u1", "rid": "r1", "group": "vip"}
    sig = signing.signature(base, SECRET, FIELDS)
    # adding tracking params not in FIELDS must not change the signature, and field order can't matter
    assert signing.signature({"group": "vip", "rid": "r1", "uid": "u1", "utm": "x"}, SECRET, FIELDS) == sig


def test_missing_signed_field_is_distinct_from_empty():
    # an absent field is encoded explicitly (null), so it differs from a value AND from an empty string —
    # dropping a signed param can't collide with a legitimately-empty one
    a = signing.signature({"uid": "u1", "rid": "r1"}, SECRET, FIELDS)               # group missing
    b = signing.signature({"uid": "u1", "rid": "r1", "group": "vip"}, SECRET, FIELDS)
    c = signing.signature({"uid": "u1", "rid": "r1", "group": ""}, SECRET, FIELDS)  # group blank
    assert len({a, b, c}) == 3
    assert signing.signature({"uid": "u1", "rid": "r1", "group": None}, SECRET, FIELDS) == a
    signed_blank = signing.sign_params({"uid": "u1", "rid": "r1", "group": ""}, SECRET, FIELDS)
    dropped = {k: v for k, v in signed_blank.items() if k != "group"}
    assert not signing.verify(dropped, SECRET, FIELDS)


def test_signed_query_is_verifiable_after_url_parse():
    from urllib.parse import parse_qs
    q = signing.signed_query({"uid": "u1", "rid": "r1", "group": "vip"}, SECRET, FIELDS)
    parsed = {k: v[0] for k, v in parse_qs(q).items()}
    assert signing.verify(parsed, SECRET, FIELDS)


# ---- N6: unambiguous canonical encoding (v2) ---------------------------------------------------

def test_delimiter_injection_cannot_forge_a_signature():
    fields = ["uid", "rid"]
    original = {"rid": "x&uid=y", "uid": "z"}
    signed = signing.sign_params(original, SECRET, fields)
    assert signing.verify(signed, SECRET, fields)
    forged = {"rid": "x", "uid": "y&uid=z", "sig": signed["sig"]}
    assert not signing.verify(forged, SECRET, fields)
    # other delimiter-shaped shifts: '=', quotes, commas, brackets, newlines, backslashes
    for a, b in [({"rid": "a=b", "uid": "c"}, {"rid": "a", "uid": "b=c"}),
                 ({"rid": 'a","uid","b', "uid": "c"}, {"rid": "a", "uid": 'b","c'}),
                 ({"rid": "a],[\"uid", "uid": "c"}, {"rid": "a", "uid": "],[c"}),
                 ({"rid": "a\n", "uid": "b"}, {"rid": "a", "uid": "\nb"}),
                 ({"rid": "a\\", "uid": "b"}, {"rid": "a", "uid": "\\b"})]:
        assert signing.signature(a, SECRET, fields) != signing.signature(b, SECRET, fields)


def test_canonical_payload_is_versioned_json_pairs():
    assert signing._canonical({"uid": "u1", "rid": "r&1"}, ["uid", "rid", "group"]) == \
        '[["group",null],["rid","r&1"],["uid","u1"]]'
    assert signing.FORMAT_PREFIX.startswith(b"survey-agent/url-sig/v2")
    import hashlib, hmac as _h
    expect = _h.new(SECRET.encode(), signing.FORMAT_PREFIX + signing._canonical(
        {"uid": "u1"}, ["uid"]).encode(), hashlib.sha256).hexdigest()
    assert signing.signature({"uid": "u1"}, SECRET, ["uid"]) == expect
    # the unprefixed (unversioned) HMAC is NOT accepted
    bare = _h.new(SECRET.encode(), signing._canonical({"uid": "u1"}, ["uid"]).encode(),
                  hashlib.sha256).hexdigest()
    assert not signing.verify({"uid": "u1", "sig": bare}, SECRET, ["uid"])


def test_unicode_values_roundtrip_and_are_distinct():
    p = {"uid": "Zoë-東京-🙂", "rid": "r\u00e9", "group": "ñ"}
    signed = signing.sign_params(p, SECRET, FIELDS)
    assert signing.verify(signed, SECRET, FIELDS)
    # NFC vs NFD forms are different bytes, so different signatures
    assert signing.signature({"uid": "\u00e9"}, SECRET, ["uid"]) != signing.signature({"uid": "e\u0301"}, SECRET, ["uid"])
    from urllib.parse import parse_qs
    q = signing.signed_query(p, SECRET, FIELDS)
    parsed = {k: v[0] for k, v in parse_qs(q).items()}
    assert signing.verify(parsed, SECRET, FIELDS)


def test_field_declaration_order_does_not_matter():
    p = {"uid": "u1", "rid": "r1", "group": "vip"}
    sig = signing.signature(p, SECRET, ["uid", "rid", "group"])
    assert signing.signature(p, SECRET, ["group", "uid", "rid"]) == sig
    assert signing.signature(p, SECRET, ["group", "uid", "rid", "uid"]) == sig   # duplicates collapse


def test_tampering_any_signed_field_is_detected():
    p = {"uid": "u1", "rid": "r1", "group": "vip"}
    signed = signing.sign_params(p, SECRET, FIELDS)
    for f in FIELDS:
        for bad in ("", p[f] + "x", p[f].upper(), None):
            forged = dict(signed)
            if bad is None:
                forged.pop(f)
            else:
                forged[f] = bad
            assert not signing.verify(forged, SECRET, FIELDS), (f, bad)
    # the signature itself tampered
    assert not signing.verify(dict(signed, sig=signed["sig"][:-1] + ("0" if signed["sig"][-1] != "0" else "1")),
                              SECRET, FIELDS)
    # an unsigned extra param is free to change
    assert signing.verify(dict(signed, utm="anything"), SECRET, FIELDS)


def test_sign_url_cli_mints_a_verifiable_url(tmp_path, monkeypatch, capsys):
    import json
    from urllib.parse import parse_qs, urlparse
    from survey_agent.cli import main
    spec = {"survey": {"id": "sig-demo", "title": "t",
                       "settings": {"url_signing": {"required": True, "fields": ["uid", "rid"]}},
                       "pages": [{"id": "p1", "questions": [{"id": "q1", "type": "text", "title": "Q"}]}]}}
    path = tmp_path / "s.survey.json"
    path.write_text(json.dumps(spec))
    monkeypatch.setenv("SURVEY_URL_SECRET", SECRET)
    assert main(["sign-url", str(path), "uid=a&uid=b", "rid=r1", "--base", "http://x"]) == 0
    url = capsys.readouterr().out.strip().splitlines()[-1]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["uid"] == "a&uid=b" and signing.verify(q, SECRET, ["uid", "rid"])
    # a blank signed value is refused: the runtime's query parser drops blank params, so it could never verify
    assert main(["sign-url", str(path), "uid=", "rid=r1", "--base", "http://x"]) == 1
