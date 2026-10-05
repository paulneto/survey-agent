"""Compile an authorable Markdown survey into the canonical *.survey.json.

Two authoring modes are supported:

  (A) Frontmatter-canonical (recommended, reliable): the YAML frontmatter holds the full
      `survey:` object; the Markdown body is human-readable notes. Compile = emit frontmatter.

  (B) Block Markdown (lightweight): frontmatter holds survey meta/settings/quotas/actions,
      and the body declares pages/questions:
          ## Page p_screen
          ### q_age (number)
          What is your age?
          - @required
          - @validation: {"min": 18, "max": 99}
          ### q_use (radio)
          Have you used it?
          - current | Currently using
          - never   | Never used
      A `- @key: <json>` line directly under `## Page` sets a page attribute, e.g.
          ## Page p_rate
          - @repeat: {"source": "q_brands"}
      For anything beyond this simple subset, author in JSON or mode (A).

Run:  survey-agent compile surveys/foo.md            # prints JSON
      survey-agent compile surveys/foo.md --write    # writes surveys/foo.survey.json
"""
import json
import re
import sys

PAGE_RE = re.compile(r"^##\s+Page\s+(\S+)\s*$")
Q_RE = re.compile(r"^###\s+(\S+)\s*\((\w+)\)\s*$")


def _split_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end].strip(), text[end + 4:]
    return "", text


def _parse_body_pages(body):
    pages, cur_page, cur_q, awaiting_title = [], None, None, False
    for raw in body.splitlines():
        line = raw.rstrip()
        m = PAGE_RE.match(line)
        if m:
            cur_page = {"id": m.group(1), "questions": []}
            pages.append(cur_page)
            cur_q, awaiting_title = None, False
            continue
        mq = Q_RE.match(line)
        if mq and cur_page is not None:
            cur_q = {"id": mq.group(1), "type": mq.group(2)}
            cur_page["questions"].append(cur_q)
            awaiting_title = True
            continue
        if cur_q is None:
            s = line.strip()
            if cur_page is not None and s.startswith("- @") and ":" in s:   # page attribute
                k, v = s[3:].split(":", 1)
                cur_page[k.strip()] = json.loads(v.strip())
            continue
        s = line.strip()
        if not s:
            continue
        if awaiting_title and not s.startswith("-"):
            cur_q["title"] = s
            awaiting_title = False
            continue
        if s.startswith("- @"):
            attr = s[3:].strip()
            if attr == "required":
                cur_q["required"] = True
            elif ":" in attr:
                k, v = attr.split(":", 1)
                cur_q[k.strip()] = json.loads(v.strip())
            continue
        if s.startswith("-"):
            opt = s[1:].strip()
            value, label = ([x.strip() for x in opt.split("|", 1)] if "|" in opt else (opt, opt))
            cur_q.setdefault("options", []).append({"value": value, "label": label})
    return pages


def compile_markdown(text):
    import yaml
    fm, body = _split_frontmatter(text)
    meta = yaml.safe_load(fm) if fm.strip() else {}
    meta = meta or {}
    body_pages = _parse_body_pages(body)

    if "survey" in meta:  # mode (A)
        survey = meta["survey"]
        if body_pages and not survey.get("pages"):
            survey["pages"] = body_pages
        return {"survey": survey}

    survey = dict(meta)  # mode (B)
    if body_pages:
        survey["pages"] = body_pages
    return {"survey": survey}


# ----------------------------------------------------------------------------- reverse: JSON → Markdown
# `render_markdown` is the inverse of `compile_markdown`: it emits a researcher-readable, logic-annotated
# Markdown document whose YAML frontmatter is the canonical survey, so compile_markdown(render_markdown(s))
# == s exactly. The body is a formatted questionnaire with the skip/branch/disqualify/quota/piping/
# display logic translated to plain language — the thing a researcher reviews in a PR.

_OP_TEXT = {
    "equals": "is", "not_equals": "is not", "contains": "contains",
    "is_one_of": "is one of", "is_not_one_of": "is not one of",
    "is_answered": "is answered", "is_not_answered": "is blank",
    "greater_than": ">", "less_than": "<", "greater_or_equal": "≥", "less_or_equal": "≤",
    "between": "is between", "regex_match": "matches", "is_full": "is full", "is_not_full": "is not full",
}
_NO_VALUE_OPS = {"is_answered", "is_not_answered", "is_full", "is_not_full"}


def _ref_text(ref):
    rid = ref.get("id")
    if ref.get("kind") == "question" and str(rid).endswith("[@]"):
        return f"this item's answer to Q `{rid[:-3]}`"
    label = {"question": f"Q `{rid}`", "url_var": f"URL `{rid}`", "variable": f"var `{rid}`",
             "quota": f"quota `{rid}`", "enrichment": f"context `{rid}`",
             "loop": f"the repeated item's {rid}"}.get(ref.get("kind"), f"`{rid}`")
    return label + (f" [row {ref['row']}]" if ref.get("row") else "")


def _val_text(v):
    return " / ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)


def _logic_text(node):
    """Translate a logic-AST node to a plain-language phrase (no eval; pure read)."""
    if not isinstance(node, dict):
        return ""
    if "op" in node and "conditions" in node:
        parts = [p for p in (_logic_text(c) for c in node["conditions"]) if p]
        inner = f" {node['op'].upper()} ".join(parts)
        return f"({inner})" if len(parts) > 1 else inner
    if "ref" in node:
        op = node.get("operator")
        base = f"{_ref_text(node['ref'])} {_OP_TEXT.get(op, op)}"
        return base if op in _NO_VALUE_OPS else f"{base} {_val_text(node.get('value'))}"
    return ""


def _calc_text(c):
    if not isinstance(c, dict):
        return f"= {c}"
    if "ref" in c:
        return f"= {_ref_text(c['ref'])}"
    return f"= {c.get('op', 'calc')}(…)"


def _repeat_text(r):
    if r.get("by") == "value":
        what = f"count in Q `{r.get('source')}` (1…N)"
    else:
        what = f"option {'not ' if r.get('mode') == 'not_selected' else ''}chosen in Q `{r.get('source')}`"
    extra = []
    if r.get("exclude"):
        extra.append("except " + ", ".join(f"`{x}`" for x in r["exclude"]))
    if r.get("max"):
        extra.append(f"max {r['max']}")
    if r.get("randomize"):
        extra.append("random order")
    return what + (f" ({'; '.join(extra)})" if extra else "")


def _question_md(q, n):
    lines, t = [], q.get("type")
    if q.get("repeat"):
        lines.append(f"🔁 _Asked once per {_repeat_text(q['repeat'])} — answers stored as `{q['id']}[item]`._")
    req = "  *(required)*" if q.get("required") else ""
    lines.append(f"### {n}. {q.get('title', q['id'])}  `{t}`{req}")
    lines.append(f"<sub>id `{q['id']}`</sub>")
    if q.get("body"):
        lines.append(("> _Instructions:_ " if t == "instructions" else "") + str(q["body"]).replace("\n", "  \n"))
    for o in q.get("options", []) or []:
        notes = []
        if o.get("exclusive"):
            notes.append("exclusive")
        if o.get("display_logic"):
            notes.append(f"shown if {_logic_text(o['display_logic'])}")
        tail = f"  _({'; '.join(notes)})_" if notes else ""
        lines.append(f"- `{o['value']}` — {o.get('label', o['value'])}{tail}")
    if q.get("options_source"):
        s = q["options_source"]
        lines.append(f"- _options piped from Q `{s['pipe_from']}` ({s.get('mode', 'selected')})_")
    if q.get("rows"):
        cols = ", ".join(str(o.get("label", o["value"])) for o in q.get("options", []))
        lines.append(f"_Grid · {q.get('cell_type', 'radio')} cells · columns: {cols}_")
        for r in q["rows"]:
            dl = f" — shown if {_logic_text(r['display_logic'])}" if r.get("display_logic") else ""
            lines.append(f"  - row `{r['id']}` — {r.get('label', r['id'])}{dl}")
    v = q.get("validation") or {}
    vn = []
    if "min" in v or "max" in v:
        vn.append(f"range {v.get('min', '–')}–{v.get('max', '–')}")
    if v.get("format") == "email":
        vn.append("email")
    if v.get("format") == "phone":
        vn.append(f"phone ({v.get('phone_region', 'intl')})")
    if v.get("percent"):
        vn.append("0–100%")
    if v.get("min_length") or v.get("max_length"):
        vn.append(f"length {v.get('min_length', 0)}–{v.get('max_length', '∞')}")
    if v.get("min_words"):
        vn.append(f"≥{v['min_words']} words")
    if v.get("regex"):
        vn.append(f"pattern `{v['regex']}`")
    if vn:
        lines.append(f"_Validation: {', '.join(vn)}._")
    if q.get("display_logic"):
        lines.append(f"➡️ _Shown only when {_logic_text(q['display_logic'])}._")
    if q.get("randomization"):
        rz = q["randomization"]
        fx = f"; fixed: {', '.join(map(str, rz.get('fixed', [])))}" if rz.get("fixed") else ""
        lines.append(f"_Options {rz.get('mode', 'shuffle')}d{fx}._")
    toks = sorted(set(re.findall(r"{{[^}]+}}", q.get("title", "") + " " + str(q.get("body") or ""))))
    if toks:
        lines.append(f"_Piped: {', '.join('`' + t + '`' for t in toks)}._")
    return lines


def render_markdown(spec):
    """JSON survey spec → readable, logic-annotated Markdown (frontmatter-canonical; round-trips)."""
    import yaml
    survey = spec.get("survey", spec)
    fm = yaml.safe_dump({"survey": survey}, sort_keys=False, allow_unicode=True, width=100).rstrip()

    body = [f"# {survey.get('title', survey['id'])}"]
    if survey.get("description"):
        body.append(f"_{survey['description']}_")
    meta = []
    if survey.get("tags"):
        meta.append("tags: " + ", ".join(str(t) for t in survey["tags"]))
    if survey.get("owner"):
        meta.append("owner: " + str(survey["owner"]))
    meta.append(f"version {survey.get('version', 1)}")
    body.append(" · ".join(meta))
    body.append("> Researcher-readable questionnaire with logic notes. The YAML frontmatter above is the "
                "**canonical source** — `survey-agent compile` regenerates the JSON from it. Edit the "
                "frontmatter (or the `.survey.json`), then re-render.")

    n = 0
    for page in survey.get("pages", []):
        body.append(f"## Page `{page['id']}`" + (f" — {page['title']}" if page.get("title") else ""))
        if page.get("display_logic"):
            body.append(f"➡️ _Page shown only when {_logic_text(page['display_logic'])}._")
        if page.get("repeat"):
            body.append(f"🔁 _Page repeats once per {_repeat_text(page['repeat'])}; its answers are "
                        f"stored per item as `qid[item]`._")
        if page.get("randomization"):
            body.append("_Question order on this page is randomized._")
        for q in page.get("questions", []):
            n += 1
            body.extend(_question_md(q, n))

    if survey.get("hidden"):
        body.append("## Hidden values (computed, never shown to the respondent)")
        for h in survey["hidden"]:
            when = f" — when {_logic_text(h['when'])}" if h.get("when") else ""
            calc = _calc_text(h["calc"]) if h.get("calc") else f"= {h.get('value')}"
            body.append(f"- `{h['id']}` {calc}{when}")

    if survey.get("quotas"):
        body.append("## Quotas")
        for qu in survey["quotas"]:
            ref = f" on Q `{qu['ref']['id']}`" if qu.get("ref") else ""
            match = f" where answer in {qu['match']}" if qu.get("match") else ""
            body.append(f"- `{qu['id']}` — cap **{qu['limit']}** ({qu['scope']}{ref}{match})")

    body.append("## Logic & terminals")
    for a in survey.get("actions", []):
        t = a.get("type")
        when = f" — when {_logic_text(a['when'])}" if a.get("when") else " — always"
        if t == "disqualify":
            msg = f" · “{a['dq_message']}”" if a.get("dq_message") else ""
            body.append(f"- ⛔ **Screen out** (reason `{a.get('reason_code', '—')}`){when}{msg}")
        elif t == "overquota":
            body.append(f"- 🪣 **Over-quota** terminal{when}")
        elif t == "skip":
            body.append(f"- ↪️ **Skip to** page `{a.get('to_page')}`{when}")
        elif t == "set_variable":
            body.append(f"- 🧮 **Set** `{a.get('name')}`{when}")
        elif t == "webhook":
            body.append(f"- 🔔 **Webhook** `{a.get('method', 'POST')}` {a.get('url')}{when}")
        elif t == "complete":
            r = a.get("redirect") or {}
            dest = r.get("task_url") or r.get("url") or r.get("kind", "default")
            body.append(f"- ✅ **Complete** → {dest}" + (when if a.get("when") else ""))

    return f"---\n{fm}\n---\n\n" + "\n\n".join(body) + "\n"


def _main():
    if len(sys.argv) < 2:
        print("usage: survey-agent compile <file.md> [--write]")
        sys.exit(2)
    path = sys.argv[1]
    with open(path) as f:
        compiled = compile_markdown(f.read())
    out = json.dumps(compiled, indent=2)
    if "--write" in sys.argv[2:]:
        outpath = re.sub(r"\.md$", ".survey.json", path)
        with open(outpath, "w") as f:
            f.write(out + "\n")
        print(f"wrote {outpath}")
    else:
        print(out)


if __name__ == "__main__":
    _main()
