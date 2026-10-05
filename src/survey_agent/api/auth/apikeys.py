"""API keys for programmatic / agent / CLI access to the API gateway.

Keys look like `sa_<token>`; only a sha256 hash is ever stored, so a leaked store can't be used to
recover live keys. The backend is pluggable: deployments with Mongo use MongoKeyStore (the engine's
Mongo), tests use InMemoryKeyStore. This is the machine half of the auth model (humans use Google
OAuth).
"""
import hashlib
import os
import secrets
import time
from typing import Optional, Protocol

PREFIX = "sa_"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _new_key() -> tuple[str, str]:
    return PREFIX + secrets.token_urlsafe(32), secrets.token_hex(8)


class KeyStore(Protocol):
    def add(self, label: str, email: Optional[str] = None) -> str: ...
    def verify(self, key: str) -> Optional[dict]: ...
    def list(self) -> list: ...
    def revoke(self, key_id: str) -> bool: ...


class InMemoryKeyStore:
    """For tests / single-process dev. Not persistent."""

    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def add(self, label: str, email: Optional[str] = None) -> str:
        key, kid = _new_key()
        self._d[_hash(key)] = {
            "id": kid, "label": label, "email": email, "created": time.time(), "disabled": False,
        }
        return key

    def verify(self, key: str) -> Optional[dict]:
        rec = self._d.get(_hash(key))
        if rec and not rec["disabled"]:
            return {"kind": "api_key", "id": rec["id"], "label": rec["label"], "email": rec.get("email")}
        return None

    def list(self) -> list:
        return [{k: r.get(k) for k in ("id", "label", "email", "created", "disabled", "last_used")}
                for r in self._d.values()]

    def revoke(self, key_id: str) -> bool:
        for rec in self._d.values():
            if rec["id"] == key_id:
                rec["disabled"] = True
                return True
        return False


class MongoKeyStore:
    """Persistent store — key hashes in the engine's Mongo (collection `api_keys`)."""

    def __init__(self, uri: str = None, db: str = None) -> None:
        from pymongo import MongoClient

        uri = uri or os.environ.get("SURVEY_MONGODB_URI", "mongodb://localhost:27017")
        db = db or os.environ.get("SURVEY_MONGODB_DB", "survey_agent")
        self._col = MongoClient(uri)[db]["api_keys"]
        self._col.create_index("key_hash", unique=True)

    def add(self, label: str, email: Optional[str] = None) -> str:
        key, kid = _new_key()
        self._col.insert_one(
            {
                "_id": kid, "key_hash": _hash(key), "label": label, "email": email,
                "created": time.time(), "disabled": False,
            }
        )
        return key

    def verify(self, key: str) -> Optional[dict]:
        rec = self._col.find_one({"key_hash": _hash(key), "disabled": False})
        if not rec:
            return None
        self._col.update_one({"_id": rec["_id"]}, {"$set": {"last_used": time.time()}})
        return {"kind": "api_key", "id": rec["_id"], "label": rec.get("label"), "email": rec.get("email")}

    def list(self) -> list:
        return [
            {
                "id": r["_id"], "label": r.get("label"), "email": r.get("email"),
                "created": r.get("created"), "disabled": r.get("disabled"), "last_used": r.get("last_used"),
            }
            for r in self._col.find()
        ]

    def revoke(self, key_id: str) -> bool:
        return self._col.update_one({"_id": key_id}, {"$set": {"disabled": True}}).modified_count > 0
