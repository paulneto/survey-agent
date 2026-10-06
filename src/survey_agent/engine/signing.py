"""HMAC signing for inbound URL variables — the pure primitive behind "signable URL params".

A drop-in `survey_url` carries identity/eligibility values as query params (uid, rid, group, …).
A respondent can edit those in the address bar, so a deployment that pays per-complete or quotas on
`group` needs to know they weren't forged. This module is the engine half: a pure, deterministic
HMAC over a declared set of fields, with a timing-safe check. It mirrors the webhook signer.

Like the rest of `engine/`, this is side-effect-free and offline: it takes a params dict + a secret
and returns/checks a signature. WHERE the secret comes from (env `SURVEY_URL_SECRET`) and WHAT to do
on a bad signature (reject, or let the survey screen the respondent out) is the caller's policy —
see `server.py` for the reference wiring and `settings.url_signing` for the per-survey switch.

    from survey_agent.engine import signing
    q = signing.sign_params({"uid": "u1", "rid": "r1", "group": "vip"}, secret, ["uid","rid","group"])
    # -> {"uid": "u1", "rid": "r1", "group": "vip", "sig": "<hex>"}  ->  emit as the survey_url query
    ok = signing.verify(incoming_params, secret, ["uid","rid","group"])   # on ingest

Signature format (v2). The HMAC input is the domain-separation prefix `survey-agent/url-sig/v2\n`
followed by the canonical JSON (compact separators, UTF-8, no ASCII escaping) of the ordered pair list
`[[name, value], ...]` — one pair per declared field, sorted by field name, de-duplicated. A value is
the field's string form; a field that is absent (or None) is encoded as JSON `null`, so a missing field
never signs the same as an empty string. Because JSON quotes and escapes every string, no choice of
values can shift a delimiter (the v1 `name=value&...` concatenation could: `rid="x&uid=y", uid="z"`
signed the same bytes as `rid="x", uid="y&uid=z"`). The payload is stable regardless of query-string
order and independent of any unsigned params. Any future change to the encoding must change the
prefix, so old and new signatures can never be confused.
"""
import hashlib
import hmac
import json

DEFAULT_PARAM = "sig"
FORMAT_PREFIX = b"survey-agent/url-sig/v2\n"


def _canonical(params, fields):
    """The exact bytes signed (excluding the version prefix): canonical JSON of the name-sorted
    `[[name, value_or_null], ...]` pairs for the declared fields. Order-independent, unambiguous, and
    blind to params outside `fields` (so adding tracking params never invalidates a signature)."""
    pairs = []
    for f in sorted({str(f) for f in fields}):
        v = params.get(f)
        pairs.append([f, None if v is None else str(v)])
    return json.dumps(pairs, separators=(",", ":"), ensure_ascii=False)


def signature(params, secret, fields):
    """The hex HMAC-SHA256 of the canonical payload. `secret` is a str or bytes shared secret."""
    key = secret.encode() if isinstance(secret, str) else secret
    msg = FORMAT_PREFIX + _canonical(params, fields).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_params(params, secret, fields, param=DEFAULT_PARAM):
    """Return a copy of `params` with the signature added under `param` (default `sig`). This is what
    an operator calls when minting a per-respondent survey_url."""
    out = dict(params)
    out[param] = signature(params, secret, fields)
    return out


def signed_query(params, secret, fields, param=DEFAULT_PARAM):
    """A ready-to-use URL query string (`uid=u1&rid=r1&group=vip&sig=…`) for the signed params."""
    from urllib.parse import urlencode
    return urlencode(sign_params(params, secret, fields, param=param))


def verify(params, secret, fields, param=DEFAULT_PARAM):
    """True iff `params[param]` matches the signature recomputed over `fields`. Timing-safe; a missing
    or non-string signature is False (never raises)."""
    provided = params.get(param)
    if not isinstance(provided, str) or not provided:
        return False
    return hmac.compare_digest(provided, signature(params, secret, fields))
