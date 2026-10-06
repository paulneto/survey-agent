"""MongoDB store — the production backend. Concurrent sessions, atomic quota increments,
idempotent session creation (unique index), indexed uid lookup, horizontal scale.

This is the backend the Django runtime uses in production (Atlas on AWS / DocumentDB). pymongo
is imported lazily so the rest of the package works without it installed.

Collections:
  sessions        unique index (survey_id, rid, uid); also index session_id, url_variables.uid
  quota_counters  unique index (survey_id, version, quota_id); $inc is atomic
  quota_reservations  one doc per reserve_quotas(key=...) outcome (`_id` = [survey, version, key])
  responses       export records, `_id` = the response id (upsert — one record per response);
                  index (survey_id, url_variables.uid)

`client=` injects an already-built client (e.g. mongomock in tests) instead of connecting to `uri`.
"""
import json

from . import (erasure_result, norm_id_fields, response_matches,
               session_matches)


def _safe_field(f):
    """A url-var name usable in a dotted Mongo path (no '.', no leading '$')."""
    return "." not in f and not f.startswith("$")


class MongoStore:
    def __init__(self, uri="mongodb://localhost:27017", db="survey_agent", client=None):
        from pymongo import ASCENDING
        if client is None:
            from pymongo import MongoClient
            client = MongoClient(uri)
        self._client = client
        self._txn_ok = None          # unknown until the first reserve_quotas (standalone → False)
        self.db = self._client[db]
        self.sessions = self.db.sessions
        self.quota_counters = self.db.quota_counters
        self.responses = self.db.responses
        self.quota_reservations = self.db.quota_reservations
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

    # ---- all-or-nothing quota reservation ----
    @staticmethod
    def _reservation_id(survey_id, version, key):
        return json.dumps([survey_id, version, str(key)])

    def _existing_reservation(self, rid, session=None):
        doc = self.quota_reservations.find_one({"_id": rid}, session=session) if session is not None \
            else self.quota_reservations.find_one({"_id": rid})
        return None if doc is None else bool(doc.get("ok"))

    def _record_reservation(self, rid, ok, session=None):
        """Insert the outcome for `rid`. Returns None when this call recorded it, else the outcome
        another worker recorded first (DuplicateKeyError on the `_id`)."""
        from pymongo.errors import DuplicateKeyError
        try:
            if session is not None:
                self.quota_reservations.insert_one({"_id": rid, "ok": bool(ok)}, session=session)
            else:
                self.quota_reservations.insert_one({"_id": rid, "ok": bool(ok)})
            return None
        except DuplicateKeyError:
            if session is not None:
                raise
            got = self._existing_reservation(rid)
            return bool(ok) if got is None else got

    def reserve_quotas(self, survey_id, version, limits, key=None):
        """Increment every bucket in `limits` iff each limited one is below its limit; all-or-nothing.

        With a replica set / mongos, one multi-document transaction does the conditional increments
        (a write conflict aborts + retries, so two reservers can't both take the last slot). On a
        standalone server (no transactions) it falls back to sequential conditional `$inc`s
        (`count < limit` in the filter) and compensates (`$inc: -1`) any already taken when a later
        one fails — never over-fills; in the brief compensation window a concurrent reserver may
        see an inflated count and be refused (a safe false "full").

        `key` (the response id) makes the call idempotent across workers: the outcome is stored in
        `quota_reservations` (inside the transaction when there is one) and a repeated call returns it
        without incrementing again. Without transactions, the increments happen first and the outcome
        doc is inserted after; a racing worker that loses the insert compensates its own increments
        and returns the winner's outcome (a crash between the two steps can leak one slot — the safe
        direction: it can refuse, never over-fill)."""
        if not limits and key is None:
            return True
        rid = None if key is None else self._reservation_id(survey_id, version, key)
        if rid is not None:
            got = self._existing_reservation(rid)
            if got is not None:
                return got
        self._ensure_counters(survey_id, version, list(limits or {}))
        if self._txn_ok is not False:
            try:
                return self._reserve_txn(survey_id, version, limits or {}, rid)
            except NotImplementedError:                    # e.g. mongomock: no sessions
                self._txn_ok = False
            except Exception as e:                         # standalone mongod: IllegalOperation (20)
                from pymongo.errors import DuplicateKeyError
                if isinstance(e, DuplicateKeyError) and rid is not None:
                    got = self._existing_reservation(rid)  # another worker recorded it first
                    if got is not None:
                        return got
                    raise
                if getattr(e, "code", None) == 20 or "Transaction numbers" in str(e) \
                        or "replica set" in str(e):
                    self._txn_ok = False
                else:
                    raise
        return self._reserve_sequential(survey_id, version, limits or {}, rid)

    def _ensure_counters(self, survey_id, version, qids):
        """Create missing counters at 0 (outside any transaction), so the conditional increments never
        need an upsert — an upsert against a full counter would collide with the unique index."""
        from pymongo.errors import DuplicateKeyError
        for qid in qids:
            try:
                self.quota_counters.update_one(
                    {"survey_id": survey_id, "version": version, "quota_id": qid},
                    {"$setOnInsert": {"count": 0}}, upsert=True)
            except DuplicateKeyError:                      # a concurrent creator won — fine
                pass

    def _cond_inc(self, survey_id, version, qid, limit, session=None):
        """+1 one (existing) counter, only if below `limit` (unconditional when None). True if done."""
        if limit is not None and limit <= 0:
            return False
        filt = {"survey_id": survey_id, "version": version, "quota_id": qid}
        if limit is not None:
            filt["count"] = {"$lt": limit}
        res = self.quota_counters.update_one(filt, {"$inc": {"count": 1}}, session=session)
        return res.modified_count == 1

    def _reserve_txn(self, survey_id, version, limits, rid=None):
        outcome = {}

        def body(session):
            outcome["ok"] = False
            for qid, lim in limits.items():
                if not self._cond_inc(survey_id, version, qid, lim, session=session):
                    session.abort_transaction()
                    return
            if rid is not None:
                self._record_reservation(rid, True, session=session)
            outcome["ok"] = True

        with self._client.start_session() as session:
            session.with_transaction(body)
        self._txn_ok = True
        if not outcome["ok"] and rid is not None:          # record the refusal (outside the aborted txn)
            got = self._record_reservation(rid, False)
            if got is not None:
                return got
        return outcome["ok"]

    def _compensate(self, survey_id, version, qids):
        for t in qids:
            self.quota_counters.update_one(
                {"survey_id": survey_id, "version": version, "quota_id": t}, {"$inc": {"count": -1}})

    def _reserve_sequential(self, survey_id, version, limits, rid=None):
        taken = []
        ok = True
        for qid, lim in limits.items():
            if self._cond_inc(survey_id, version, qid, lim):
                taken.append(qid)
                continue
            self._compensate(survey_id, version, taken)    # compensate: no partial reservation
            taken, ok = [], False
            break
        if rid is not None:
            got = self._record_reservation(rid, ok)
            if got is not None:                            # another worker's outcome stands
                self._compensate(survey_id, version, taken)
                return got
        return ok

    # ---- responses ----
    def record_response(self, survey_id, response):
        """Upsert keyed by the response `id` (stored as `_id`, so the server enforces uniqueness): a
        re-recorded terminal — retried after a failure, or processed by two hosts — replaces its
        document, never adds a second one. A record without an id is inserted."""
        from pymongo.errors import DuplicateKeyError
        doc = {"survey_id": survey_id, **response}
        rid = response.get("id")
        if rid is None:
            self.responses.insert_one(doc)
            return
        doc["_id"] = str(rid)
        try:
            self.responses.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        except DuplicateKeyError:                          # two concurrent upserts: the retry replaces
            self.responses.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    @staticmethod
    def _id_query(prefix, respondent_id, fields):
        vals = list({str(respondent_id), respondent_id}) if not isinstance(respondent_id, str) \
            else [respondent_id]
        if not all(_safe_field(f) for f in fields):
            return None                                    # odd var name → scan, filter in Python
        return {"$or": [{f"{prefix}.{f}": {"$in": vals}} for f in fields]}

    def list_responses(self, survey_id=None, uid=None, id_fields=None):
        fields = norm_id_fields(id_fields)
        q = {}
        if survey_id is not None:
            q["survey_id"] = survey_id
        if uid is not None:
            if fields == ("uid",):
                q["url_variables.uid"] = str(uid)
            else:
                iq = self._id_query("url_variables", uid, fields)
                if iq:
                    q.update(iq)
        out = []
        for d in self.responses.find(q):
            d.pop("_id", None)
            if uid is None or fields == ("uid",) or response_matches(d, uid, fields):
                out.append(d)
        return out

    def _find(self, coll, prefix, respondent_id, fields, survey_id, match):
        q = self._id_query(prefix, respondent_id, fields) or {}
        if survey_id is not None:
            q["survey_id"] = survey_id
        out = []
        for d in coll.find(q):
            if match(d, respondent_id, fields):
                out.append(d)
        return out

    def find_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        fields = norm_id_fields(id_fields)
        resp = self._find(self.responses, "url_variables", respondent_id, fields, survey_id,
                          response_matches)
        sess = self._find(self.sessions, "url_vars", respondent_id, fields, survey_id, session_matches)
        for d in resp + sess:
            d.pop("_id", None)
        return {"responses": resp, "sessions": sess}

    def erase_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        """DSAR erasure: delete every matching response AND session (partial sessions, retakes, every
        survey). Returns counts + the upload refs / webhook keys found in what was deleted."""
        fields = norm_id_fields(id_fields)
        resp = self._find(self.responses, "url_variables", respondent_id, fields, survey_id,
                          response_matches)
        sess = self._find(self.sessions, "url_vars", respondent_id, fields, survey_id, session_matches)
        if resp:
            self.responses.delete_many({"_id": {"$in": [d["_id"] for d in resp]}})
        if sess:
            self.sessions.delete_many({"_id": {"$in": [d["_id"] for d in sess]}})
        for d in resp + sess:
            d.pop("_id", None)
        return erasure_result(resp, sess)

    def delete_responses(self, uid=None, survey_id=None):
        """Compat wrapper. With a uid this is a full respondent erasure (responses AND sessions, matched
        on url var `uid`); with only survey_id it drops that survey's responses. Returns responses removed."""
        if uid is not None:
            return self.erase_respondent(uid, ("uid",), survey_id)["responses"]
        q = {} if survey_id is None else {"survey_id": survey_id}
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
