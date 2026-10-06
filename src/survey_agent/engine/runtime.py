"""Server-driven page-flow runtime — storage- and framework-agnostic decision logic.

The caller (Django runtime, the local demo, or the simulator) owns persistence
(Mongo / in-memory) and side effects (firing panel callbacks, atomically incrementing
quota counters). This module is a pure function over a `session` dict: it decides what to
render next and which terminal/callback to fire.

Usage:
    rt = SurveyRuntime(unwrap(spec))           # spec may be {"survey": {...}} or bare
    session = rt.new_session(session_id, url_vars)
    step = rt.start(session, quota_state)      # -> {"page": ...} | {"terminal": ...}
    step = rt.submit(session, page_id, answers, quota_state)
    # on a 'complete' terminal: caller atomically reserves rt.quota_buckets(session) (else
    # rt.overquota_terminal(session)) and fires callback
"""
import hashlib
import re

from .ast_eval import EvalContext, evaluate, resolve_ref
from . import piping
from . import safety as _safety
from . import repeat
from .randomize import seeded_order

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_DISQUALIFIED = "disqualified"
STATUS_OVERQUOTA = "overquota"
STATUS_USER_TERMINATED = "user_terminated"
STATUS_EXPIRED = "expired"

TERMINAL_STATUSES = {
    STATUS_COMPLETED, STATUS_DISQUALIFIED, STATUS_OVERQUOTA,
    STATUS_USER_TERMINATED, STATUS_EXPIRED,
}

# generic delivery events; engine/terminal.py maps status->event, integrations/ deliver them
# question types that are stimulus/content, not inputs (no answer expected)
DISPLAY_ONLY = {"video", "image", "instructions"}
# answered by picking from `options`; one with no options left to pick (option piping from an empty
# answer, or every option hidden by its display_logic) is skipped instead of shown unanswerable
CHOICE_TYPES = {"radio", "checkbox", "dropdown", "dropdown_multi", "ranking"}

EVENT_STARTED = "started"
EVENT_COMPLETED = "completed"
EVENT_TERMINATED = "terminated"
EVENT_TERMINATED_BY_USER = "terminated_by_user"

# Default identity url-var names used when a survey doesn't set settings.identity. The engine ships the
# generic uid/rid; a deployment can override these (e.g. a panel's own pid/tid) by assigning the module
# attributes from its config/CLI layer (see cli.main). The pure engine never reads the environment.
# Page renders one synthetic walk (simulator / sample data) may take before it's called stuck — a floor;
# SurveyRuntime.max_steps raises it to cover every pass of every repeated page.
MAX_PAGES = 1000

DEFAULT_RESPONDENT_VAR = "uid"
DEFAULT_RESPONSE_VAR = "rid"


def unwrap(spec):
    """Accept either {"survey": {...}} or a bare survey dict; return the survey dict."""
    return spec.get("survey", spec) if isinstance(spec, dict) else spec


def _phone_ok(value, region=None):
    """Validate a phone number as US, UK, or international (E.164). `region`: 'us' | 'uk' | 'intl'
    (default 'intl' — accepts any of the three). Separators (spaces, -, (), .) are ignored, so the
    respondent can type it naturally; a leading + marks an international number."""
    import re
    s = str(value).strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return False
    us = len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))
    uk = (len(digits) in (10, 11) and digits.startswith("0")) or \
         (digits.startswith("44") and 11 <= len(digits) <= 13)
    e164 = has_plus and 8 <= len(digits) <= 15
    region = (region or "intl").lower()
    if region == "us":
        return us or (e164 and digits.startswith("1"))
    if region == "uk":
        return uk or (e164 and digits.startswith("44"))
    return us or uk or e164


def response_id(session):
    """The unique, system-generated identity for this response — used as the stored response id *and*
    its terminal lifecycle event/idempotency key. Minted once at session start by the persisting
    caller (see server) and carried on the session, so it's distinct for every response (including
    each retake) and independent of the inbound identity vars (uid/rid), which are data only.
    Falls back to `session_id` for synthetic, non-persisted sessions (simulation, chat preview) that
    don't mint one."""
    return session.get("response_id") or session.get("session_id")


class SurveyRuntime:
    def __init__(self, definition):
        self.d = unwrap(definition)
        self.pages = self.d.get("pages", [])
        self.page_index = {p["id"]: i for i, p in enumerate(self.pages)}
        # Both maps also resolve repeat instance ids (`q_rate[netflix]` -> `q_rate`), so callers that
        # look a submitted/stored answer key up here never need to know about repeating.
        self.questions_by_id = repeat.InstanceIndex(
            (q["id"], q) for p in self.pages for q in p.get("questions", [])
        )
        self.actions = self.d.get("actions", [])
        self.label_lut = repeat.InstanceIndex(piping.build_label_lookup(self.d))
        # the most page renders one respondent can see: every page, plus every pass of every page repeat
        self.max_steps = max(MAX_PAGES, len(self.pages) + sum(
            repeat.cap(p["repeat"]) for p in self.pages if p.get("repeat")) + 10)
        # question id -> the repeat spec its answers are keyed by (its own, or its page's)
        self.repeat_of = {}
        for p in self.pages:
            for q in p.get("questions", []):
                spec = p.get("repeat") or q.get("repeat")
                if isinstance(spec, dict):
                    self.repeat_of[q["id"]] = spec
        self.settings = self.d.get("settings", {})
        # Which URL variables carry identity. Fully configurable per survey via
        # settings.identity; defaults to the generic uid/rid. `respondent` is the per-person id
        # (free for dedup, behavioral enrichment, and DSAR/erasure); `response` is the per-invite id
        # that keys the resumable session. Neither keys the stored response — that uses a
        # system-minted response_id (see response_id()). The engine never assumes a particular name.
        _ident = self.settings.get("identity") or {}
        self.respondent_var = _ident.get("respondent", DEFAULT_RESPONDENT_VAR)
        self.response_var = _ident.get("response", DEFAULT_RESPONSE_VAR)
        # Retake policy: "once" (default — a finished invite link is locked) or "multiple"
        # (returning after finishing starts a fresh take; each completion is its own response, with
        # its own system-minted response_id).
        self.retake = (self.settings.get("retake") or "once")

    def identity(self, url_vars):
        """Return (respondent_id, response_id) pulled from url_vars using this survey's configured
        identity variable names. Callers (runtime/store/enrichment) use this instead of hardcoding
        a parameter name, so a deployment can use uid/rid, pid/tid, or anything it likes."""
        uv = url_vars or {}
        return uv.get(self.respondent_var), uv.get(self.response_var)

    # ------------------------------------------------------------------ session
    def new_session(self, session_id, url_vars, response_id=None):
        return {
            "session_id": session_id,   # resume handle: survey:<response-var>:<respondent-var> (unchanged)
            "response_id": response_id, # unique per response; minted at session start by the persisting
                                        # caller. None for synthetic sessions → response_id() falls back
                                        # to session_id.
            "survey_id": self.d["id"],
            "version": self.d.get("version", 1),
            "url_vars": dict(url_vars or {}),
            "variables": {},
            "answers": {},
            "page_pointer": -1,
            "furthest_index": -1,
            "status": STATUS_IN_PROGRESS,
            "reason_code": None,
            "render_state": {},
            "enrichments": {},   # behavioral facts (Firebolt/cache), resolved once at session start
            # which captured url vars may leave the system in started/terminal passback events: the
            # survey's declared settings.url_variables + the identity vars. Everything the link
            # carried is still captured in url_vars (the drop-in contract); this only scopes egress.
            "passback_vars": self.passback_vars(),
        }

    def passback_vars(self):
        """Declared url variables (settings.url_variables) plus the identity vars, in order, deduped —
        the allowlist of captured params forwarded to integrations (see engine/terminal.build_event)."""
        out = []
        for v in list(self.settings.get("url_variables") or []) + [self.respondent_var, self.response_var]:
            if isinstance(v, str) and v and v not in out:
                out.append(v)
        return out

    def _ctx(self, session, quota_state=None, loop=None):
        return EvalContext(
            answers=session["answers"],
            url_vars=session["url_vars"],
            variables=session["variables"],
            quotas=quota_state or {},
            enrichments=session.get("enrichments", {}),
            loop=loop,
        )

    # -------------------------------------------------------------- repeating
    def repeat_items(self, spec, session, salt):
        """The items a repeat iterates over for this session: [{value, label, index, total}].
        Labels are piped; `randomize` shuffles with a per-session seed (stable across resume)."""
        ctx = self._ctx(session)
        its = repeat.items(spec, session["answers"], self.questions_by_id)
        if spec.get("randomize"):
            its = seeded_order(its, lambda x: x["value"], f'{session["session_id"]}|repeat|{salt}')
        n = len(its)
        return [{"value": it["value"], "label": piping.resolve_text(it["label"], ctx, self.label_lut),
                 "index": i + 1, "total": n} for i, it in enumerate(its)]

    def instance_item(self, key):
        """For a stored answer key `q_rate[netflix]`: {"source", "value", "label"} of its repeat
        item (labels from the source question). None for an ordinary answer key."""
        base, item = repeat.split_instance(key)
        spec = self.repeat_of.get(base)
        if item is None or not spec:
            return None
        if spec.get("by") == "value":
            label = item
        else:
            label = self.label_lut.get(spec.get("source"), {}).get(item, item)
        return {"source": spec.get("source"), "value": item, "label": label}

    def answer_keys(self, qid, seen):
        """The stored-answer keys for question `qid`, given the keys present in some responses
        (`seen`): [qid] for a plain question; its instance keys `qid[item]`, in source-item order,
        for a repeated one. Lets reports and flat exports give each item its own column/chart."""
        spec = self.repeat_of.get(qid)
        if not spec:
            return [qid]
        keys = {k for k in seen if repeat.split_instance(k)[0] == qid and repeat.split_instance(k)[1]}
        if spec.get("by") == "value":
            def order(k):
                item = repeat.split_instance(k)[1]
                return int(item) if item.isdigit() else float("inf")
        else:
            idx = {str(o.get("value")): i for i, o in
                   enumerate(repeat.source_options(spec.get("source"), self.questions_by_id))}
            def order(k):
                return idx.get(repeat.split_instance(k)[1], len(idx))
        return sorted(keys, key=order)

    def instance_loop(self, key, session=None):
        """The loop item {value, label, index?, total?} a stored instance `qid[item]` was asked with —
        exactly as the respondent saw it when `session` recorded it, else rebuilt from the definition
        (a by-value item's index is its value; an answer item's index isn't knowable without a session)."""
        seen = ((session or {}).get("loops") or {}).get(key)
        if seen:
            return dict(seen)
        it = self.instance_item(key)
        if not it:
            return None
        loop = {"value": it["value"], "label": it["label"]}
        if (self.repeat_of.get(repeat.split_instance(key)[0]) or {}).get("by") == "value" \
                and it["value"].isdigit():
            loop["index"] = int(it["value"])
        return loop

    def instance_title(self, key, title, session=None):
        """A repeated question's title for one stored instance (reports/export/insights): loop tokens
        piped with that item (fallbacks honoured; other tokens left as authored). If the result doesn't
        name the item, its label is appended so per-item columns and charts stay distinct."""
        loop = self.instance_loop(key, session)
        if not loop:
            return title
        def sub(m):
            kind, rest, fallback = piping.split_token(m.group(1))
            if kind != "loop":
                return m.group(0)
            v = loop.get(rest)
            return str(v) if v not in (None, "") else (fallback or "")
        out = re.sub(r"[ \t]{2,}", " ", piping.TOKEN.sub(sub, title or "")).strip()
        return out if str(loop["label"]) in out else f"{out} — {loop['label']}"

    def _loop(self, page, session):
        """The current item of a repeated page, or None."""
        lp = session.get("loop")
        if not page.get("repeat") or not lp or lp.get("page") != page["id"]:
            return None
        its = lp.get("items") or []
        return its[lp["index"]] if lp.get("index", 0) < len(its) else None

    def _instances(self, page, session, quota_state=None):
        """Every question instance shown on this page: [(answer_key, question, ctx)], in page order,
        display_logic applied per instance. A plain question is one instance keyed by its id; a
        repeated question (or any question on a repeated page) is keyed `qid[item]` and evaluated
        with that item bound as the loop."""
        loop = self._loop(page, session)
        base_ctx = self._ctx(session, quota_state, loop=loop)
        out = []
        for q in page.get("questions", []):
            if q.get("repeat") and not loop:
                shown = [it for it in self.repeat_items(q["repeat"], session, q["id"])
                         if self._shown(q, base_ctx.with_loop(it))]
                for i, it in enumerate(shown):          # {{loop:index}} of {{loop:total}} counts what's shown
                    ctx = base_ctx.with_loop({**it, "index": i + 1, "total": len(shown)})
                    out.append((repeat.instance_id(q["id"], it["value"]), q, ctx))
            elif self._shown(q, base_ctx):
                key = repeat.instance_id(q["id"], loop["value"]) if loop else q["id"]
                out.append((key, q, base_ctx))
        return out

    def _shown(self, q, ctx):
        if not evaluate(q.get("display_logic"), ctx):
            return False
        if q["type"] in CHOICE_TYPES and (q.get("options") or q.get("options_source")):
            return bool(self._options(q, ctx))
        return True

    def _options(self, q, ctx):
        """A question's selectable options for this respondent: piped (`options_source`) or static,
        filtered by option display_logic. Labels are not yet piped."""
        src = q.get("options", [])
        if q.get("options_source"):
            source_opts = repeat.source_options(q["options_source"]["pipe_from"], self.questions_by_id)
            src = piping.resolve_options_source(q, ctx, source_opts)
        return [o for o in src if evaluate(o.get("display_logic"), ctx)]

    def blank_tokens(self, session):
        """[(where, token)] merge tokens on the current page that render empty for this respondent and
        have no `|fallback` — the gaps a respondent would see. Used by the simulator's coverage report."""
        if session.get("page_pointer", -1) < 0 or session.get("status") != STATUS_IN_PROGRESS:
            return []
        page = self.pages[session["page_pointer"]]
        loop = self._loop(page, session)
        out = [("page title", t) for t in
               piping.blank_tokens(page.get("title"), self._ctx(session, loop=loop), self.label_lut)]
        for key, q, ctx in self._instances(page, session):
            texts = [("title", q.get("title")), ("body", q.get("body"))]
            texts += [(f"option {o.get('value')}", o.get("label")) for o in self._options(q, ctx)]
            texts += [(f"row {r.get('id')}", r.get("label")) for r in q.get("rows", [])
                      if evaluate(r.get("display_logic"), ctx)]
            for field, text in texts:
                out += [(f"{key} {field}", t) for t in piping.blank_tokens(text, ctx, self.label_lut)]
        return out

    def _enter(self, idx, session, quota_state=None):
        """Move to page `idx` (which must be eligible) and render it; a repeated page starts its loop
        at the first item that has anything to show."""
        page = self.pages[idx]
        session["page_pointer"] = idx
        session["furthest_index"] = max(session["furthest_index"], idx)
        session.pop("loop", None)
        if page.get("repeat"):
            session["loop"] = {"page": page["id"], "index": 0,
                               "items": self._live_items(page, session, quota_state)}
        return {"ok": True, "page": self.render_page(page, session, quota_state)}

    def _live_items(self, page, session, quota_state=None):
        """A repeated page's items, minus those where every question is hidden (e.g. a follow-up
        gated on `q_rate[@]`), renumbered so {{loop:index}} of {{loop:total}} counts what's shown."""
        its = self.repeat_items(page["repeat"], session, page["id"])
        live = [it for i, it in enumerate(its)
                if self._instances(page, {**session, "loop": {"page": page["id"], "index": i, "items": its}},
                                   quota_state)]
        return [{**it, "index": i + 1, "total": len(live)} for i, it in enumerate(live)]

    def _settle_loop(self, page, session, quota_state=None):
        """Skip forward past iterations where every question is hidden (items are filtered on entry,
        so this only matters if a variable changed mid-loop). Returns False once the loop is done."""
        lp = session["loop"]
        while lp["index"] < len(lp["items"]):
            if self._instances(page, session, quota_state):
                return True
            lp["index"] += 1
        return False

    # -------------------------------------------------------------- page flow
    def _page_eligible(self, page, session, quota_state=None):
        if not evaluate(page.get("display_logic"), self._ctx(session, quota_state)):
            return False
        if page.get("repeat"):     # a repeated page with nothing to show for any item is skipped
            return bool(self._live_items(page, session, quota_state))
        # a page whose every question is hidden (or left with no options) is skipped, not shown blank
        return bool(self._instances(page, session, quota_state)) or not page.get("questions")

    def _first_eligible_from(self, start_idx, session, quota_state=None):
        for i in range(start_idx, len(self.pages)):
            if self._page_eligible(self.pages[i], session, quota_state):
                return i
        return None

    def start(self, session, quota_state=None):
        idx = self._first_eligible_from(0, session, quota_state)
        if idx is None:
            return {"ok": True, "terminal": self._terminal(session, {"type": "complete"})}
        return self._enter(idx, session, quota_state)

    def current_page(self, session, quota_state=None):
        """Re-render the page the session is currently on (resume / error re-render)."""
        if session["page_pointer"] < 0:
            return self.start(session, quota_state)
        return {"ok": True, "page": self.render_page(self.pages[session["page_pointer"]], session, quota_state)}

    def submit(self, session, page_id, raw_answers, quota_state=None):
        """Process a page submission.

        Returns one of:
          {"ok": False, "errors": {...}, "page": rendered}   re-render with errors
          {"ok": True,  "page": rendered}                    next page
          {"ok": True,  "terminal": {...}}                   terminal + callback event
        """
        # Forward-only flow: never accept a submission for a page other than the one the respondent
        # is currently on. Page flow advances monotonically (page_pointer), so a submission for an
        # already-passed page — i.e. the browser Back button — would otherwise let a respondent
        # re-answer an earlier question and shop for answers that slip past a screen-out. Reject any
        # stale/unknown page id and just re-render where they actually are: no answers change, no
        # logic re-runs. (The terminal page itself is guarded by the caller refusing to submit a
        # session whose status is already terminal.)
        # A repeated page's id carries its item (`p_rate[netflix]`), so a stale submission from an
        # earlier iteration is caught the same way.
        base_id, item = repeat.split_instance(page_id)
        if self.page_index.get(base_id) != session["page_pointer"]:
            return self.current_page(session, quota_state)
        page = self.pages[self.page_index[base_id]]
        loop = self._loop(page, session)
        if (loop["value"] if loop else None) != item:
            return self.current_page(session, quota_state)
        errors = self.validate_answers(page, raw_answers, session, quota_state)
        if errors:
            return {"ok": False, "errors": errors, "page": self.render_page(page, session, quota_state)}

        # Store every submitted answer that belongs to this page. We trust the renderer to
        # only submit visible questions (server-driven page-at-a-time: a question's visibility
        # depends on prior-page answers, which are already in the session). Filtering by
        # display_logic here would use a pre-store context and drop legitimately-shown answers.
        page_questions = {key: q for key, q, _ in self._instances(page, session, quota_state)}
        for qid, val in raw_answers.items():
            if qid in page_questions:
                session["answers"][qid] = self._coerce(page_questions[qid], val)

        self._apply_set_variables(session)
        self._apply_hidden(session)
        hooks = self._collect_webhooks(session)   # webhook actions whose `when` just became true

        if loop:
            # Mid-loop: a terminal (disqualify / over-quota / complete) can still end the survey after
            # any iteration, but skips only apply once the loop is done.
            session["loop"]["index"] += 1
            if self._settle_loop(page, session, quota_state):
                decision = self._evaluate_actions(session, quota_state,
                                                  kinds=("disqualify", "overquota", "complete"))
                if decision:
                    return self._attach(hooks, {"ok": True, "terminal": self._terminal(session, decision)})
                return self._attach(hooks, {"ok": True, "page": self.render_page(page, session, quota_state)})
            session.pop("loop", None)

        decision = self._evaluate_actions(session, quota_state)
        if decision and decision["type"] in ("disqualify", "overquota", "complete"):
            return self._attach(hooks, {"ok": True, "terminal": self._terminal(session, decision)})

        next_idx = None
        if decision and decision["type"] == "skip":
            next_idx = self.page_index.get(decision["to_page"])
            if next_idx is not None and not self._page_eligible(self.pages[next_idx], session, quota_state):
                next_idx = self._first_eligible_from(next_idx, session, quota_state)
        if next_idx is None:
            next_idx = self._first_eligible_from(self.page_index[base_id] + 1, session, quota_state)
        if next_idx is None:
            return self._attach(hooks, {"ok": True, "terminal": self._terminal(session, {"type": "complete"})})

        return self._attach(hooks, self._enter(next_idx, session, quota_state))

    @staticmethod
    def _attach(hooks, result):
        if hooks:
            result["webhooks"] = hooks   # caller delivers these (outbox + retry in prod)
        return result

    def terminate_by_user(self, session, reason=None):
        session["status"] = STATUS_USER_TERMINATED
        session["reason_code"] = reason or "user_terminated"
        return {"status": STATUS_USER_TERMINATED, "event": EVENT_TERMINATED_BY_USER,
                "reason_code": session["reason_code"]}

    # ----------------------------------------------------------- rendering
    def render_page(self, page, session, quota_state=None):
        loop = self._loop(page, session)
        ctx = self._ctx(session, loop=loop)
        instances = self._instances(page, session, quota_state)
        for key, _, qctx in instances:     # remember the exact item each instance was asked with (export)
            if qctx.loop and key not in (q["id"] for q in page.get("questions", [])):
                session.setdefault("loops", {})[key] = dict(qctx.loop)
        questions = [self._render_question(q, session, qctx, key) for key, q, qctx in instances]
        rnd = page.get("randomization")
        if rnd:
            # `fixed` names authored question ids; match an instance by its base id
            questions = seeded_order(
                questions, lambda x: repeat.split_instance(x["id"])[0],
                f'{session["session_id"]}|page|{page["id"]}',
                mode=rnd.get("mode"), fixed=rnd.get("fixed"),
            )
        out = {
            "page_id": repeat.instance_id(page["id"], loop["value"]) if loop else page["id"],
            "questions": questions,
        }
        self._pipe_into(out, "title", page.get("title", ""), ctx)
        if loop:
            out["iteration"] = dict(loop)          # {value, label, index, total} — "2 of 3" UI
        return out

    def _pipe_into(self, out, field, text, ctx):
        """Set out[field] to the piped plain text, and out[field + "_md"] to the copy a Markdown renderer
        must use when it differs (untrusted piped values escaped — see piping.resolve_text)."""
        out[field] = piping.resolve_text(text, ctx, self.label_lut, display=True)
        md = piping.resolve_text(text, ctx, self.label_lut, markdown=True, display=True)
        if md != out[field]:
            out[field + "_md"] = md
        return out

    def _render_question(self, q, session, ctx, key=None):
        key = key or q["id"]
        rq = {"id": key, "type": q["type"], "required": q.get("required", False)}
        self._pipe_into(rq, "title", q.get("title", ""), ctx)
        if q.get("options") or q.get("options_source"):
            visible = self._options(q, ctx)
            options = [self._pipe_into({"value": o["value"], "exclusive": o.get("exclusive", False)},
                                       "label", str(o.get("label", o["value"])), ctx) for o in visible]
            rnd = q.get("randomization")
            if rnd:
                options = seeded_order(
                    options, lambda x: x["value"],
                    f'{session["session_id"]}|q|{q["id"]}',
                    mode=rnd.get("mode"), fixed=rnd.get("fixed"),
                )
            rq["options"] = options
        if q.get("rows"):
            rq["rows"] = [self._pipe_into({"id": r["id"]}, "label", str(r.get("label", "")), ctx)
                          for r in q["rows"] if evaluate(r.get("display_logic"), ctx)]
        if q.get("scale_points"):
            rq["scale_points"] = q["scale_points"]
        if q.get("body"):                        # instructions / rich description (Markdown, piped)
            self._pipe_into(rq, "body", q["body"], ctx)
        if q.get("media"):                       # video / image stimulus content (rendered by the frontend)
            rq["media"] = q["media"]
        if q.get("validation"):
            rq["validation"] = q["validation"]
        if ctx.loop and key != q["id"]:
            rq["repeat_item"] = dict(ctx.loop)       # which item this instance is about
        if key in session["answers"]:
            rq["answer"] = session["answers"][key]   # prefill on resume / error re-render
        return rq

    # ----------------------------------------------------------- validation
    def validate_answers(self, page, raw_answers, session, quota_state=None):
        errors = {}
        for qid, q, ctx in self._instances(page, session, quota_state):
            if q["type"] in DISPLAY_ONLY:          # video / instructions are stimulus, not inputs
                continue
            val = raw_answers.get(qid)
            blank = val is None or val == "" or val == []
            if q.get("required") and blank:
                errors[qid] = "This question is required."
                continue
            if blank:
                continue
            v = q.get("validation") or {}
            import re as _re
            if q["type"] == "number":
                try:
                    n = float(val)
                except (TypeError, ValueError):
                    errors[qid] = "Please enter a number."
                    continue
                if n != n or n in (float("inf"), float("-inf")):      # nan / inf parse as floats
                    errors[qid] = "Please enter a number."
                    continue
                lo = 0 if v.get("percent") else v.get("min")
                hi = 100 if v.get("percent") else v.get("max")
                if lo is not None and n < lo:
                    errors[qid] = f"Must be at least {lo}."
                elif hi is not None and n > hi:
                    errors[qid] = f"Must be at most {hi}."
            elif q["type"] in ("text", "textarea"):
                s = str(val)
                if "min_length" in v and len(s) < v["min_length"]:
                    errors[qid] = f"Must be at least {v['min_length']} characters."
                elif "max_length" in v and len(s) > v["max_length"]:
                    errors[qid] = f"Must be at most {v['max_length']} characters."
                elif "min_words" in v and len(s.split()) < v["min_words"]:
                    errors[qid] = f"Please answer in at least {v['min_words']} words."
                elif v.get("format") == "email" and not _re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", s):
                    errors[qid] = v.get("format_message", "Please enter a valid email address.")
                elif v.get("format") == "phone" and not _phone_ok(s, v.get("phone_region")):
                    errors[qid] = v.get("format_message", "Please enter a valid phone number (US, UK, or +country code).")
                elif "regex" in v and _safety.safe_search(v["regex"], s) is False:
                    # bounded match (engine/safety.py); a pattern the validator would reject is
                    # skipped rather than trapping the respondent on an unanswerable question.
                    errors[qid] = v.get("regex_message", "Invalid format.")
            elif q["type"] == "date":
                if not _re.match(r"\d{4}-\d{2}-\d{2}$", str(val)):
                    errors[qid] = "Please enter a date (YYYY-MM-DD)."
                elif v.get("min") and str(val) < v["min"]:
                    errors[qid] = f"Must be on or after {v['min']}."
                elif v.get("max") and str(val) > v["max"]:
                    errors[qid] = f"Must be on or before {v['max']}."
            elif q["type"] == "grid" and q.get("required"):
                rowvals = val if isinstance(val, dict) else {}
                needed = [r["id"] for r in q.get("rows", []) if evaluate(r.get("display_logic"), ctx)]
                if any(not rowvals.get(rid) for rid in needed):
                    errors[qid] = "Please answer every row."
            elif q["type"] == "file":
                # The answer is an upload *reference* (filename/size/key/url) — the engine never
                # sees the bytes. The web layer pre-validates the same metadata with
                # file_upload_error() BEFORE storing anything, then stores and submits the refs.
                err = self.file_upload_error(q, val)
                if err:
                    errors[qid] = err
        return errors

    @staticmethod
    def file_upload_error(q, val):
        """Validate a `file` answer's metadata (a ref dict, or a list of them — only `filename` and
        `size` are read): count cap, extension allow-list, per-file size cap. Returns an error message
        or None. Pure — callers can run it on {filename, size} stubs before persisting any bytes."""
        v = q.get("validation") or {}
        refs = val if isinstance(val, list) else [val]
        exts = [e.lower().lstrip(".") for e in (v.get("extensions") or [])]
        max_kb = v.get("max_size_kb")
        max_files = v.get("max_files", 1)
        if len(refs) > max_files:
            return f"Please upload at most {max_files} file(s)."
        for ref in refs:
            name = (ref or {}).get("filename", "") if isinstance(ref, dict) else ""
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if exts and ext not in exts:
                return f"File type .{ext or '?'} not allowed (allowed: {', '.join(exts)})."
            size = (ref or {}).get("size") if isinstance(ref, dict) else None
            if max_kb and isinstance(size, (int, float)) and size > max_kb * 1024:
                return f"File is too large (max {max_kb} KB)."
        return None

    def file_questions_for_submit(self, session, page_id, quota_state=None):
        """{answer_key: question} for the `file` question instances displayed on the page the
        respondent is currently on — or {} when `page_id` is stale/unknown (submit() would just
        re-render). The web layer stores uploaded bytes only for these keys, so a forged or stale
        multipart POST can't park blobs against questions that aren't on screen."""
        base_id, item = repeat.split_instance(page_id or "")
        if base_id not in self.page_index or self.page_index[base_id] != session.get("page_pointer"):
            return {}
        page = self.pages[self.page_index[base_id]]
        loop = self._loop(page, session)
        if (loop["value"] if loop else None) != item:
            return {}
        return {key: q for key, q, _ in self._instances(page, session, quota_state) if q["type"] == "file"}

    # ----------------------------------------------------------- internals
    def _coerce(self, q, val):
        if q["type"] == "number":
            try:
                f = float(val)
                return int(f) if f.is_integer() else f
            except (TypeError, ValueError):
                return val
        if q["type"] in ("checkbox", "ranking", "dropdown_multi") and not isinstance(val, list):
            val = [val]
        if q["type"] == "checkbox" and isinstance(val, list):
            # exclusive option ("none of the above" / "n/a") wins: collapse to it, dropping co-selected
            # ones. Enforced here in the pure engine so it holds for every renderer/runtime, not just
            # the client JS that also clears them.
            excl = {str(o.get("value")) for o in q.get("options", []) if o.get("exclusive")}
            picked = [v for v in val if str(v) in excl]
            if picked:
                return picked
        return val

    def _apply_set_variables(self, session):
        ctx = self._ctx(session)
        for a in self.actions:
            if a.get("type") != "set_variable":
                continue
            if not evaluate(a.get("when"), ctx):
                continue
            if "value" in a:
                session["variables"][a["name"]] = a["value"]
            elif "value_from" in a:
                # Copy the stored (coded) answer, preserving lists — NOT a piped label string. A
                # variable derived from a coded/multi answer must stay comparable in later logic
                # (equals / is_one_of against option values); label-piping would silently break that.
                session["variables"][a["name"]] = resolve_ref(
                    {"kind": "question", "id": a["value_from"]}, ctx)
            ctx = self._ctx(session)  # refresh so later vars can chain

    def _apply_hidden(self, session):
        """Compute declared `hidden` values (calculations) into session variables — stored on the
        response (export) and usable in logic/piping/quotas/routing. Data, not code (engine/calc.py)."""
        from . import calc
        for h in self.d.get("hidden", []):
            ctx = self._ctx(session)
            if h.get("when") and not evaluate(h["when"], ctx):
                continue
            if "calc" in h:
                session["variables"][h["id"]] = calc.evaluate(h["calc"], ctx)
            elif "value" in h:
                session["variables"][h["id"]] = h["value"]

    def _collect_webhooks(self, session):
        """Resolve `webhook` actions whose `when` just became true (fire-once per session). Returns
        delivery intents {method, url, payload} with merge tokens resolved. The engine never makes a
        network call — the caller delivers these (with an outbox + retries in production)."""
        ctx = self._ctx(session)
        fired = session.setdefault("fired_webhooks", [])
        out = []
        for i, a in enumerate(self.actions):
            if a.get("type") != "webhook" or i in fired:
                continue
            if not evaluate(a.get("when"), ctx):
                continue
            fired.append(i)
            payload = {k: piping.resolve_text(str(v), ctx, self.label_lut)
                       for k, v in (a.get("payload") or {}).items()}
            # Stable idempotency key so a delivering caller (or its receiver) can dedupe under retry.
            # Keyed by (session, action index): fire-once is per-session-per-action, mirroring the
            # terminal-event key in engine/terminal.py.
            key = hashlib.sha256(f'{session["session_id"]}|webhook:{i}'.encode()).hexdigest()
            out.append({"method": a.get("method", "POST").upper(),
                        "url": piping.resolve_text(a.get("url", ""), ctx, self.label_lut),
                        "payload": payload, "idempotency_key": key})
        return out

    def _evaluate_actions(self, session, quota_state, kinds=("disqualify", "overquota", "skip", "complete")):
        """Return the first matching action. Precedence: disqualify, overquota, skip,
        complete. Only actions with an explicit `when` fire during the flow; a `complete`
        action without `when` is the default end-redirect (used when pages are exhausted)."""
        ctx = self._ctx(session, quota_state)
        for kind in kinds:
            for a in self.actions:
                if a.get("type") == kind and a.get("when") and evaluate(a["when"], ctx):
                    return a
        return None

    def _default_redirect(self):
        for a in self.actions:
            if a.get("type") == "complete" and not a.get("when"):
                return a.get("redirect", {"kind": "default"})
        return {"kind": "default"}

    def _resolve_redirect(self, redirect, session):
        if not redirect:
            return {"kind": "default"}
        r = dict(redirect)
        ctx = self._ctx(session)
        for key in ("task_url", "url"):
            if key in r:
                r[key] = piping.resolve_text(r[key], ctx, self.label_lut)
        return r

    def _terminal(self, session, action):
        t = action["type"]
        if t == "complete":
            session["status"] = STATUS_COMPLETED
            redirect = self._resolve_redirect(action.get("redirect") or self._default_redirect(), session)
            return {"status": STATUS_COMPLETED, "event": EVENT_COMPLETED, "redirect": redirect}
        ctx = self._ctx(session)
        if t == "disqualify":
            session["status"] = STATUS_DISQUALIFIED
            session["reason_code"] = action.get("reason_code")
            return {
                "status": STATUS_DISQUALIFIED, "event": EVENT_TERMINATED,
                "reason_code": action.get("reason_code"),
                "message": piping.resolve_text(action.get("dq_message", "Thanks for your time."), ctx, self.label_lut),
                "redirect": self._resolve_redirect(action.get("redirect") or {"kind": "default"}, session),
            }
        if t == "overquota":
            session["status"] = STATUS_OVERQUOTA
            session["reason_code"] = "overquota"
            return {
                "status": STATUS_OVERQUOTA, "event": EVENT_TERMINATED, "reason_code": "overquota",
                "message": piping.resolve_text(action.get("message", "This study is now full."), ctx, self.label_lut),
                "redirect": self._resolve_redirect(action.get("redirect") or {"kind": "default"}, session),
            }
        raise ValueError(f"non-terminal action: {t!r}")

    def overquota_terminal(self, session):
        """Turn this session into an over-quota terminal using the survey's own overquota action
        (its message/redirect), else a default one. Used by the caller when a `complete` loses the
        race for the last quota slot (the atomic reservation failed)."""
        action = next((a for a in self.actions if a.get("type") == "overquota"), None) or {"type": "overquota"}
        return self._terminal(session, {**action, "type": "overquota"})

    # ----------------------------------------------------------- quotas
    def quota_buckets(self, session):
        """Quota ids this session's answers fall into. On a *complete* terminal the caller atomically
        reserves all of these (all-or-nothing, against their limits) before accepting the completion
        (survey-scope quotas always apply)."""
        ctx = self._ctx(session)
        hit = []
        for qd in self.d.get("quotas", []):
            if qd.get("scope") == "survey":
                hit.append(qd["id"])
                continue
            ref = qd.get("ref")
            actual = resolve_ref(ref, ctx) if ref else None
            vals = set(map(str, actual if isinstance(actual, list) else [actual]))
            if any(str(m) in vals for m in qd.get("match", [])):
                hit.append(qd["id"])
        return hit

    def initial_quota_state(self):
        """A fresh in-memory quota_state for the demo/simulator (caller persists in prod)."""
        return {qd["id"]: {"count": 0, "limit": qd.get("limit")} for qd in self.d.get("quotas", [])}
