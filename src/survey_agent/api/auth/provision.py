"""Server-to-server provisioning endpoint for per-user API keys.

A mint-only surface used by a trusted upstream SSO broker to obtain a per-user gateway key (``sa_``)
on behalf of a Google-SSO-verified user, so the user's CLI/agent access runs on their own labelled
key — preserving per-user revocation and the ``last_used`` audit on the key store.

``POST /provision/cli-token``

- **Auth**: ``Authorization: Bearer <provisioning key>`` compared in constant time against
  ``config.PROVISION_API_KEY`` (``SURVEY_PROVISION_API_KEY``). This credential can ONLY mint — it is
  never accepted on any ``/v1`` route, and no ``sa_`` key can satisfy it (different secret). When the
  setting is unset/empty the feature is off: every call returns 404.
- **Body**: ``{"email": "<user>", "label": "cli"}`` — label optional, defaulting to ``cli``. The
  provisioning key is the trust boundary: the broker has already verified the email, so we mint on its
  say-so.
- **200**: ``{"token": "<plaintext>", "id": "<key id>", "email": "<user>", "label": "<label>"}`` —
  always a FRESH key. Plaintexts are never stored, so we never return an existing one; only its hash
  is written to the key store, bound to the user's email.

Never logs the plaintext token or the provisioning key.
"""
from __future__ import annotations

import logging
import secrets
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from survey_agent.api.auth import apikeys, config
from survey_agent.api.auth.deps import get_key_store

logger = logging.getLogger(__name__)

DEFAULT_LABEL = "cli"

router = APIRouter(prefix="/provision", tags=["provision"])


class ProvisionTokenRequest(BaseModel):
    email: str = Field(..., min_length=1)
    label: str = DEFAULT_LABEL


class ProvisionTokenResponse(BaseModel):
    token: str
    id: str
    email: str
    label: str


def _check_provisioning_key(authorization: Optional[str]) -> None:
    """Authorize the caller against the provisioning key.

    Disabled (404) when the key is unset/empty so the feature is off by default. 401 on a
    missing/malformed/incorrect bearer. Constant-time comparison via ``secrets.compare_digest``.
    """
    configured = config.PROVISION_API_KEY
    if not configured:
        # Feature off — don't even reveal the endpoint exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed bearer token")
    if not secrets.compare_digest(token, configured):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid provisioning key")


@router.post("/cli-token", response_model=ProvisionTokenResponse)
def provision_cli_token(
    req: ProvisionTokenRequest,
    authorization: Annotated[Optional[str], Header()] = None,
    store: apikeys.KeyStore = Depends(get_key_store),
) -> ProvisionTokenResponse:
    _check_provisioning_key(authorization)
    # Same mint logic as /keys, but bound to the broker-verified email rather than a free-text label.
    email = req.email.strip()
    label = (req.label or DEFAULT_LABEL).strip() or DEFAULT_LABEL
    # Diff the id set around the mint so we report the new key's id robustly even if a same-email/label
    # key already exists. ids are unique (token_hex(8)).
    before = {k.get("id") for k in store.list()}
    plaintext = store.add(label, email=email)
    after = {k.get("id") for k in store.list()}
    new_ids = after - before
    key_id = next(iter(new_ids), "")
    # Log the mint (id + who) — never the plaintext token or the provisioning key.
    logger.info("provision: minted key for email=%s label=%s id=%s", email, label, key_id)
    return ProvisionTokenResponse(token=plaintext, id=key_id, email=email, label=label)
