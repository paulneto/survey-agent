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
    # a field that's absent signs as empty — and that's a *different* signature from one with a value,
    # so dropping a signed param can't collide with a legitimately-empty one
    a = signing.signature({"uid": "u1", "rid": "r1"}, SECRET, FIELDS)               # group missing
    b = signing.signature({"uid": "u1", "rid": "r1", "group": "vip"}, SECRET, FIELDS)
    assert a != b


def test_signed_query_is_verifiable_after_url_parse():
    from urllib.parse import parse_qs
    q = signing.signed_query({"uid": "u1", "rid": "r1", "group": "vip"}, SECRET, FIELDS)
    parsed = {k: v[0] for k, v in parse_qs(q).items()}
    assert signing.verify(parsed, SECRET, FIELDS)
