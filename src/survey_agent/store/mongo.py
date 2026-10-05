"""MongoDB store — the production backend. Concurrent sessions, atomic quota increments,
idempotent session creation (unique index), indexed uid lookup, horizontal scale.

This is the backend the Django runtime uses in production (Atlas on AWS / DocumentDB). pymongo
is imported lazily so the rest of the package works without it installed.

Collections:
  sessions        unique index (survey_id, rid, uid); also index session_id, url_variables.uid
  quota_counters  unique index (survey_id, version, quota_id); $inc is atomic
  responses       append-only export records; index (survey_id, url_variables.uid)
"""


class MongoStore:
    def __init__(self, uri="mongodb://localhost:27017", db="survey_agent"):
        from pymongo import MongoClient, ASCENDING
        self._client = MongoClient(uri)
        self.db = self._client[db]
        self.sessions = self.db.sessions
        self.quota_counters = self.db.quota_counters
        self.responses = self.db.responses
        # idempotency + lookup indexes
        self.sessions.create_index([("survey_id", ASCENDING), ("rid", ASCENDING), ("uid", ASCENDING)], unique=True)
        self.sessions.create_index("session_id", unique=True)
        self.sessions.create_index("url_variables.uid")
        self.quota_counters.create_index(
            [("survey_id", ASCENDING), ("version", ASCENDING), ("quota_id", ASCENDING)], unique=True)
        self.responses.create_index([("survey_id", ASCENDING), ("url_variables.uid", ASCENDING)])

    def get_or_create_session(self, survey_id, rid, uid, factory):
        from pymongo.errors import DuplicateKeyError
        existing = self.sessions.find_one({"survey_id": survey_id, "rid": str(rid), "uid": str(uid)})
        if existing:
            existing.pop("_id", None)
            return existing, False
        session = factory()
        session.setdefault("rid", str(rid))
        session.setdefault("uid", str(uid))
        try:
            self.sessions.insert_one(dict(session))
            return session, True
        except DuplicateKeyError:  # lost a creation race -> return the winner
            doc = self.sessions.find_one({"survey_id": survey_id, "rid": str(rid), "uid": str(uid)})
            doc.pop("_id", None)
            return doc, False

    def save_session(self, session):
        self.sessions.update_one({"session_id": session["session_id"]}, {"$set": session}, upsert=True)

    def get_session_by_id(self, session_id):
        doc = self.sessions.find_one({"session_id": session_id})
        if doc:
            doc.pop("_id", None)
        return doc

    def quota_counts(self, survey_id, version):
        return {d["quota_id"]: d.get("count", 0)
                for d in self.quota_counters.find({"survey_id": survey_id, "version": version})}

    def incr_quota(self, survey_id, version, quota_id):
        # atomic — the authoritative quota gate in production
        doc = self.quota_counters.find_one_and_update(
            {"survey_id": survey_id, "version": version, "quota_id": quota_id},
            {"$inc": {"count": 1}},
            upsert=True,
            return_document=True,  # pymongo.ReturnDocument.AFTER
        )
        return (doc or {}).get("count", 1)

    def record_response(self, survey_id, response):
        self.responses.insert_one({"survey_id": survey_id, **response})

    def list_responses(self, survey_id=None, uid=None):
        q = {}
        if survey_id is not None:
            q["survey_id"] = survey_id
        if uid is not None:
            q["url_variables.uid"] = str(uid)
        out = []
        for d in self.responses.find(q):
            d.pop("_id", None)
            out.append(d)
        return out

    def delete_responses(self, uid=None, survey_id=None):
        """Erasure hook (DSAR): delete responses matching uid and/or survey_id. Returns count removed."""
        q = {}
        if uid is not None:
            q["url_variables.uid"] = str(uid)
        if survey_id is not None:
            q["survey_id"] = survey_id
        return self.responses.delete_many(q).deleted_count

    def ping(self):
        """Liveness probe for preflight/healthcheck — a real round-trip to the server."""
        self.db.command("ping")

    def count_responses(self, survey_id):
        """Fast status tally via count_documents (no full-document pull) — used by the console board."""
        base = {"survey_id": survey_id}
        return {"complete": self.responses.count_documents({**base, "status": "Complete"}),
                "disqualified": self.responses.count_documents({**base, "status": "Disqualified"}),
                "total": self.responses.count_documents(base)}

    def count_responses_by_survey(self):
        """Status tallies for EVERY survey in one aggregation — so the console board is a single round-trip
        instead of N count queries. Returns {survey_id: {complete, disqualified, total}}."""
        out = {}
        for d in self.responses.aggregate([
                {"$group": {"_id": {"sid": "$survey_id", "st": "$status"}, "n": {"$sum": 1}}}]):
            sid, st, n = d["_id"].get("sid"), d["_id"].get("st"), d["n"]
            b = out.setdefault(sid, {"complete": 0, "disqualified": 0, "total": 0})
            b["total"] += n
            if st == "Complete":
                b["complete"] += n
            elif st == "Disqualified":
                b["disqualified"] += n
        return out
