"""Conversational survey mode — an AI administers a survey as a chat, using the SAME survey JSON as
the spec. This is the most agentic surface, and it falls out of the architecture for free: the
engine already separates *logic* (data) from *rendering*, so the conversation is just a different
front-end over the identical `SurveyRuntime`.

Division of labour (important): the **engine stays the source of truth** for flow, branching,
screen-out, quotas, and validation. The **LLM only** (a) phrases each question conversationally and
(b) maps the respondent's free-text reply to a valid answer. The runner submits that answer to the
engine, which decides what's next. Works offline with the StubLLM; natural with a real key.

Usage:
    convo = Conversation(spec, url_vars={"uid": "1", "rid": "1"})
    print(convo.opening()["message"])         # assistant asks the first question
    out = convo.reply("yeah I use Netflix")   # -> next assistant message (or done)
"""
from survey_agent.engine.runtime import SurveyRuntime
import survey_agent.ai as ai


class Conversation:
    def __init__(self, definition, session_id="convo", url_vars=None, llm=None, enrichments=None):
        self.rt = SurveyRuntime(definition)
        self.llm = llm or ai.get_llm()
        self.session = self.rt.new_session(session_id, url_vars or {})
        if enrichments:
            self.session["enrichments"] = enrichments
        self.quota = self.rt.initial_quota_state()
        self._step = self.rt.start(self.session, self.quota)
        self._pending = {}        # answers accumulated for the current page
        self._awaiting = None     # the question id we last asked
        self._awaiting_q = None   # the *rendered* (piped) question we last asked — use this, not the raw def
        self._preamble = []       # display-only stimulus (video/instructions) shown before next ask
        # adaptive probing (opt-in, budgeted, audited): one ephemeral follow-up on a thin open-text
        # answer. It NEVER changes flow/logic — the schema answer is already accepted; the probe + its
        # reply are stored as a derived field. Off unless the survey opts in.
        _ai = (self.rt.d.get("settings", {}) or {}).get("ai", {}) or {}
        self._probe_all = bool(_ai.get("probe_open_text"))
        self._probe_budget = int(_ai.get("max_probes", 2))
        self._probe_min_words = int(_ai.get("probe_min_words", 6))
        self._probe = None        # {qid, question} while awaiting a follow-up reply

    # ---- public ----
    def opening(self):
        # conversational mode always uses an LLM to read replies, so disclose it up front
        import survey_agent.consent as consent
        step = self._advance()
        if not step.get("done"):
            step["message"] = consent.convo_notice(self.rt.d) + "\n\n" + step["message"]
        return step

    def reply(self, user_text):
        if self._probe is not None:                      # this reply answers an ephemeral follow-up
            self._record_probe(self._probe, user_text)
            self._probe = None
            return self._advance()
        if self._awaiting is None:                       # already terminal
            return self._terminal_message(self._step.get("terminal", {"status": "completed"}))
        q = self._awaiting_q or self.rt.questions_by_id.get(self._awaiting, {})  # piped, so re-asks pipe too
        answer = self.llm.interpret(q, user_text)
        empty = answer is None or answer == "" or answer == [] or answer == {}
        if empty and q.get("required"):
            return {"done": False, "message": "Sorry, I didn't quite catch that — " + self._ask(q),
                    "question_id": q["id"], "clarify": True}
        if not empty:
            self._pending[q["id"]] = answer
            follow = self._maybe_probe(q, user_text)     # one budgeted follow-up on a thin open-text
            if follow:
                self._probe = {"qid": q["id"], "question": follow}
                self._awaiting, self._awaiting_q = None, None
                return self._flush(follow, question_id=q["id"], probe=True)
        return self._advance()

    # ---- internals ----
    def _is_thin(self, text):
        t = (text or "").strip().lower()
        low_effort = {"idk", "i don't know", "dunno", "nothing", "none", "na", "n/a",
                      "no", "no comment", "not sure", "maybe"}
        return t in low_effort or len(t.split()) < self._probe_min_words

    def _maybe_probe(self, q, user_text):
        raw = self.rt.questions_by_id.get(q.get("id"), q)   # raw def carries `probe`/`type` (page payload may not)
        if self._probe_budget <= 0 or raw.get("type") not in ("text", "textarea"):
            return None
        if not (raw.get("probe") or self._probe_all) or not self._is_thin(user_text):
            return None
        try:
            follow = self.llm.probe(raw, user_text)         # opt-in + fail-open: never block on this
        except Exception:
            return None
        if follow:
            self._probe_budget -= 1
        return follow

    def _record_probe(self, probe, reply_text):
        # audited derived field — the schema answer is untouched; this is bonus open-end depth
        self.session.setdefault("probes", []).append(
            {"qid": probe["qid"], "question": probe["question"], "answer": reply_text})
    def _ask(self, q):
        msg = self.llm.phrase(q, history=None)
        if q.get("options"):
            msg += "  (" + " / ".join(str(o["label"]) for o in q["options"]) + ")"
        return msg

    def _stimulus(self, q):
        # video/image/instructions: shown, not asked. In chat we surface title + body + any media link
        # (the body is left as Markdown — chat clients render it).
        url = (q.get("media") or {}).get("url")
        bits = [q.get("title", ""), q.get("body", ""), (f"▶ {url}" if url else "")]
        return "\n\n".join(b for b in bits if b)

    def _flush(self, msg, **extra):
        full = ("\n\n".join(self._preamble) + "\n\n" + msg) if self._preamble else msg
        self._preamble = []
        return {"done": False, "message": full, **extra}

    def _advance(self):
        from survey_agent.engine.runtime import DISPLAY_ONLY
        while True:
            if "terminal" in self._step:
                return self._terminal_message(self._step["terminal"])
            page = self._step["page"]
            if page.get("title") and page["page_id"] != getattr(self, "_titled", None):
                self._titled = page["page_id"]                # a page's title (e.g. a repeat's item) once
                self._preamble.append(f"**{page['title']}**")
            nxt = next((qq for qq in page["questions"] if qq["id"] not in self._pending), None)
            if nxt is None:                                  # page complete -> submit real answers
                answers = {k: v for k, v in self._pending.items() if v is not None}
                step = self.rt.submit(self.session, page["page_id"], answers, self.quota)
                self._pending = {}
                if step.get("errors"):                       # validation: re-ask the first bad question
                    self._step = {"page": step["page"]}
                    bad = next(iter(step["errors"]))
                    badq = next((qq for qq in step["page"]["questions"] if qq["id"] == bad),
                                self.rt.questions_by_id.get(bad, {}))   # the rendered/piped question
                    self._awaiting, self._awaiting_q = bad, badq
                    return self._flush(step["errors"][bad] + " " + self._ask(badq), clarify=True)
                self._step = step
                continue
            if nxt["type"] in DISPLAY_ONLY:                  # show stimulus, don't await an answer
                self._preamble.append(self._stimulus(nxt))
                self._pending[nxt["id"]] = None
                continue
            self._awaiting, self._awaiting_q = nxt["id"], nxt
            return self._flush(self._ask(nxt), question_id=nxt["id"])

    def _terminal_message(self, terminal):
        self._awaiting = None
        msg = terminal.get("message") or {
            "completed": "Thanks — all done!",
            "disqualified": "Thanks for your time.",
            "overquota": "This study is now full — thank you!",
        }.get(terminal.get("status"), "Thanks!")
        if self._preamble:
            msg = "\n\n".join(self._preamble) + "\n\n" + msg
            self._preamble = []
        return {"done": True, "status": terminal.get("status"), "message": msg,
                "redirect": terminal.get("redirect"), "session": self.session}
