"""survey-agent command-line interface.

    survey-agent author    "<brief>" [--slug S] [--simulate N]   draft a survey from plain language (LLM)
    survey-agent copy      <src> --slug S [--title T]   duplicate an existing survey (fresh id + version)
    survey-agent templatize <src> [--id ID] [--out DIR]   promote a survey into a reusable template
    survey-agent validate  <survey.json>            schema + semantic lint
    survey-agent critique  <survey.json>            AI methodology review (leading/double-barreled/opt-outs)
    survey-agent simulate  <survey.json> [-n N] [--quota K]   random coverage gate (every path reachable)
    survey-agent pretest   <survey.json> [--n N] [--audience F] [--trait k=v]   persona respondents (incidence preview)
    survey-agent compile   <survey.md> [--write]    Markdown -> canonical JSON
    survey-agent render    <survey.json> [--write]  canonical JSON -> readable, logic-annotated Markdown
    survey-agent launch    <survey.json> [--base U] [--approve TOKEN]  emit the drop-in survey_url + mark live
    survey-agent publish   <survey.json> [--approve TOKEN]   store the definition + mark live (SURVEY_DEFINITION_SOURCE)
    survey-agent sign-url  <survey.json> uid=.. rid=.. [--base U]   mint a signed per-respondent survey_url
    survey-agent schema                             print the canonical Survey JSON Schema
    survey-agent console   [--port P] [--no-browser]   local operator console: visual board (status/reports/surveys/templates) on 127.0.0.1
    survey-agent serve     [--survey F] [--port P] [--preview]   run the survey runtime (--preview: drafts too, local only)
    survey-agent demo      [--quota K] [--no-browser]   clickable local sandbox (every state) + webhook receiver
    survey-agent convo     [--survey F] [--scripted]    take a survey as an AI conversation
    survey-agent report    <survey.json> [--simulate N] [--out F] [--open] [--insights]   HTML report
    survey-agent insights  <survey.json> [--simulate N]   AI analyst read-out (findings/themes/next steps)
    survey-agent export    <survey.json> [--simulate N] [--out F] [--format csv|parquet]   tabular export
    survey-agent init      [dir]                    scaffold a config + example survey
    survey-agent list      [--dir D] [--json]       all surveys + purpose + lifecycle (validated/tested/live)
    survey-agent status    <id> [--set S] [--approve TOKEN] [--note T] [--url U]   show/set one survey's lifecycle state
    survey-agent defs-export [--all] / defs-import / defs-diff   definition-store backup / rebuild / reconcile
    survey-agent dashboard [--live|--reports|--simulate N] [--base-url U] [--open]   HTML management dashboard
    survey-agent templates · new <tpl> --slug S     list templates · scaffold a survey from one
    survey-agent copy <id|file> --slug S · templatize <id|file>   duplicate a survey · promote it to a template
    survey-agent backup · restore <file>            response backup to JSONL · idempotent reload
    survey-agent dsar <uid> [--delete] · dsar --resume   find / erase one subject's data · finish pending erasures
    survey-agent outbox status|list|retry           the durable webhook outbox
    survey-agent api [--host H] [--port P]          the authenticated API gateway ([api] extra)
    survey-agent docs [name] · selftest              documentation map · prove the install works
    survey-agent doctor                             diagnose the install + config (flags the multi-clone trap)
    survey-agent help                               guided overview of the commands + the author→launch→report flow
"""
import argparse
import json
import os
import re
import shutil
import sys
import threading
import time

PKG = os.path.dirname(os.path.abspath(__file__))
PERSONA_N = 8           # persona respondents used for the author loop's incidence check
INCIDENCE_BAND = 30     # ± percentage points tolerated vs the brief's target before flagging
EXAMPLE = os.path.join(PKG, "examples", "example.survey.json")


def _load_dotenv(path=".env"):
    """Pick up ANTHROPIC_API_KEY etc. from a local .env so the LLM-powered CLI works without exporting
    by hand. Thin wrapper over the shared loader (also used by the MCP server)."""
    from survey_agent import load_dotenv
    return load_dotenv(path)


def _llm_hint():
    """Friendly nudge: the demo/convo run offline with a deterministic stub; tell the user how to
    turn on the real thing. Never blocks — AI features are opt-in."""
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("SURVEY_LLM", "auto").lower() != "stub":
        print("ℹ  No ANTHROPIC_API_KEY found — AI features use the offline stub.\n"
              "   Add it to a .env file (ANTHROPIC_API_KEY=sk-ant-...) or `export` it for natural\n"
              "   conversational phrasing and LLM analysis of open-text answers.\n")


def _parse_target_incidence(brief):
    """Pull a target incidence (% expected to qualify/complete) out of a plain-language brief — e.g.
    'incidence 40%', '40% incidence', 'expect ~30% to qualify'. Returns an int 1..100, or None."""
    if not brief:
        return None
    for pat in (r"incidence[^\d%]{0,15}(\d{1,3})",
                r"(\d{1,3})\s*%?\s*incidence",
                r"(\d{1,3})\s*%[^\d]{0,15}(?:qualif|complet|pass|eligible)",
                r"(?:qualif|complet|eligible)[a-z]*[^\d%]{0,15}(\d{1,3})\s*%"):
        m = re.search(pat, brief, re.I)
        if m and 1 <= int(m.group(1)) <= 100:
            return int(m.group(1))
    return None


def _parse_target_loi(brief):
    """Pull a target length-of-interview in minutes from a brief — 'under 3 min', '3-minute survey',
    '5 minutes', 'LOI 4'. Returns an int (minutes) or None. (Won't match 'minimum'.)"""
    if not brief:
        return None
    m = (re.search(r"(\d{1,2})\s*-?\s*min(?:ute)?s?\b", brief, re.I)
         or re.search(r"\bloi[^\d]{0,8}(\d{1,2})", brief, re.I))
    return int(m.group(1)) if m else None


def _cmd_author(a):
    """Agentic authoring as a CLOSED LOOP: draft → validate → *simulate* → feed the gaps back to the
    model → repeat, until the survey is schema-valid, every path works when the engine runs it
    (qualified respondents finish; every declared screen-out fires), AND — when the brief implies a
    target incidence — a realistic persona pre-test qualifies at roughly that rate. The simulator is
    the agent's ground-truth feedback: the thing a one-shot 'ask an LLM for a survey JSON' can't do.
    Uses your own Claude or Codex/OpenAI key (or an offline stub that emits a minimal screener)."""
    from survey_agent import ai
    from survey_agent.engine import validate as v
    brief = " ".join(a.brief).strip()
    schema = v.load_schema()
    llm = ai.get_llm()
    if llm.name == "stub":
        print("ℹ  No Claude/Codex key found — authoring with the OFFLINE STUB (a minimal screener).\n"
              "   For a full survey from your brief, set ANTHROPIC_API_KEY or OPENAI_API_KEY (then\n"
              "   re-run). See the .env scaffolded by `survey-agent init`.\n")
    print(f"authoring with: {llm.name}")
    target = a.target_incidence if a.target_incidence is not None else _parse_target_incidence(brief)
    target_loi = a.target_loi if a.target_loi is not None else _parse_target_loi(brief)
    if target is not None:
        print(f"target incidence: ~{target}% — each valid draft is persona pre-tested against it")
    if target_loi is not None:
        print(f"target length: ~{target_loi} min — each valid draft's estimated LOI is checked")

    spec, prior, problems = None, None, None
    attempts = max(1, a.max_repairs + 1)
    ok = False
    last_cov, last_incidence = None, None
    for i in range(attempts):
        try:
            spec = llm.author(brief, schema, prior=prior, errors=problems)
        except Exception as e:
            print(f"  draft {i + 1}: model error — {e}")
            return 1
        if isinstance(spec, dict) and "survey" not in spec:    # normalize a bare survey object
            spec = {"survey": spec}

        # gate 1 — schema + semantic validation
        errs = [e for e in v.validate(spec) if not e.startswith("[warn]")]
        if errs:
            tail = "repairing…" if i + 1 < attempts else "giving up"
            print(f"  draft {i + 1}: {len(errs)} validation error(s) — {tail}")
            prior, problems = spec, errs
            continue

        # gate 2 — coverage simulation (the closed loop). Skipped only with --simulate 0.
        if not a.simulate:
            print(f"  draft {i + 1}: valid ✅  (coverage check skipped: --simulate 0)")
            ok = True
            break
        from survey_agent import simulate
        cov = simulate.coverage(spec, n=a.simulate)
        last_cov = cov
        gaps = [f"[coverage] {p}" for p in cov["problems"]]
        loi_min = (cov.get("loi_seconds") or 0) / 60
        loi_txt = f"  ·  ~{loi_min:.1f} min LOI" if cov.get("loi_seconds") else ""

        # gate (length) — estimated LOI vs target (cheap; from the coverage run, 25% tolerance)
        if not gaps and target_loi and loi_min > target_loi * 1.25:
            gaps.append(f"[loi] estimated length ~{loi_min:.1f} min exceeds the ~{target_loi} min "
                        f"target — remove or shorten questions/pages")

        # gate 3 — realistic incidence vs the brief (only when a target is known). Persona pre-test
        # the qualified rate; a gross miss is a fixable signal that the screen-outs are mis-tuned.
        inc_txt = ""
        if not gaps and target is not None:
            measured = round(100 * simulate.persona_pretest(spec, n=PERSONA_N, audience=a.audience)["incidence"])
            last_incidence = measured / 100
            inc_txt = f"  ·  persona incidence ~{measured}% (target ~{target}%)"
            if measured == 0 and target > 0:
                gaps.append(f"[incidence] a realistic persona pre-test qualified 0% but the brief "
                            f"targets ~{target}% — the screen-outs are too strict or contradictory; "
                            f"loosen the qualifying conditions")
            elif abs(measured - target) > INCIDENCE_BAND:
                tip = ("too few qualify — loosen or remove a screen-out" if measured < target
                       else "too many qualify — add or tighten a screen-out")
                gaps.append(f"[incidence] a realistic persona pre-test qualified ~{measured}% but the "
                            f"brief targets ~{target}% ({tip})")

        if not gaps:
            print(f"  draft {i + 1}: valid ✅  ·  ✅ coverage gate passed  "
                  f"({cov['statuses'].get('completed', 0)}/{cov['n']} complete, "
                  f"{len(cov['fired_reasons'])}/{len(cov['defined_reasons'])} screen-outs firing){inc_txt}{loi_txt}")
            ok = True
            break
        tail = "repairing…" if i + 1 < attempts else "giving up"
        print(f"  draft {i + 1}: valid, but {len(gaps)} gap(s) — {tail}")
        for g in gaps:
            print(f"      • {g}")
        # stop early if the model can't make progress (returns an identical draft)
        if prior is not None and json.dumps(spec, sort_keys=True) == json.dumps(prior, sort_keys=True):
            print("      (identical draft returned — stopping; needs a manual edit or the add-logic skill)")
            break
        prior, problems = spec, gaps

    slug = a.slug or (spec.get("survey", {}).get("id") if isinstance(spec, dict) else None) or "draft"
    os.makedirs(a.out, exist_ok=True)
    outp = os.path.join(a.out, f"{slug}.survey.json")
    open(outp, "w").write(json.dumps(spec, indent=2) + "\n")
    print(f"\nwrote {outp}")
    # also emit the readable, logic-annotated Markdown (the researcher-reviewable version; round-trips)
    try:
        from survey_agent.engine.compile_md import render_markdown
        mdp = os.path.join(a.out, f"{slug}.md")
        open(mdp, "w").write(render_markdown(spec))
        print(f"wrote {mdp}  (readable questionnaire + logic notes)")
    except Exception as e:
        print(f"  (could not write readable .md: {e})")
    # register the new survey so `survey-agent list` knows it exists, was validated, and (if simulated) tested
    sid = _survey_id(spec)
    if sid:
        _record(outp, lambda r, sdir: r.record_validation(sid, ok=ok, sdir=sdir))
        if last_cov is not None:
            _record(outp, lambda r, sdir: r.record_test(sid, last_cov, incidence=last_incidence, sdir=sdir))
    if not ok:
        print("⚠  not launch-ready yet — schema, coverage, or incidence gaps remain above. Edit it "
              "(or use the add-logic skill), or re-run `survey-agent author` with a clearer brief.")
        return 1
    print(f"\nnext:  survey-agent demo --survey {outp}   (try it)"
          f"\n       human review + approve, then your launch flow   (go live)")
    return 0


def _cmd_schema(a):
    from survey_agent.engine.validate import SCHEMA_PATH
    print(open(SCHEMA_PATH).read())
    return 0


def _survey_id(spec):
    return ((spec.get("survey", spec) if isinstance(spec, dict) else {}) or {}).get("id")


def _snapshot_launched(survey_path, sid):
    """With file definitions, write the immutable snapshot of the version being launched NOW, next to the
    survey (surveys/.versions/), so it is committed and deployed with it. Otherwise the snapshot would
    only be created on the serving host — lost on a redeploy to ephemeral disk, stranding respondents
    who started on this version. Best-effort; prints what to commit."""
    try:
        from survey_agent.definitions.file import FileDefinitionStore
        sdir = os.path.dirname(os.path.abspath(survey_path))
        spec = FileDefinitionStore(sdir).get_definition(sid)
        ver = ((spec or {}).get("survey") or {}).get("version")
        if ver is not None and os.path.isdir(os.path.join(sdir, ".versions", sid)):
            print(f"   Snapshot of v{ver} saved under {os.path.relpath(os.path.join(sdir, '.versions', sid))}/ "
                  f"— commit it with the survey so in-flight respondents stay pinned across deploys.")
    except Exception:
        pass


def _record(survey_path, fn, *args):
    """Best-effort registry update keyed off a survey file's own dir — never breaks the command."""
    try:
        from survey_agent import registry
        sdir = os.path.dirname(os.path.abspath(survey_path)) or None
        return fn(registry, sdir, *args)
    except Exception:
        return None


def _resolve_survey(arg):
    """Resolve a survey given as a file path OR a bare slug. A path to an existing file wins; else
    look for `<slug>.survey.json` in the surveys dir. Returns a path, or None if nothing matches.
    Lets `copy`/`templatize` accept `welcome` (a slug in the surveys dir) or `surveys/welcome.survey.json`."""
    if not arg:
        return None
    if os.path.isfile(arg):
        return arg
    from survey_agent import registry
    sdir = registry.surveys_dir()
    cand = os.path.join(sdir, f"{arg}.survey.json")
    if os.path.isfile(cand):
        return cand
    # the survey id `list` / `status` show (it needn't match the filename, e.g. init's example.survey.json)
    try:
        for e in registry.catalog():
            if e.get("id") == arg and e.get("file"):
                path = os.path.join(sdir, e["file"])
                if os.path.isfile(path):
                    return path
    except Exception:
        pass
    return None


def _cmd_validate(a):
    from survey_agent.engine import validate as v
    spec = json.load(open(a.survey))
    errs = v.validate(spec)
    real = [e for e in errs if not e.startswith("[warn]")]
    if getattr(a, "json", False):
        print(json.dumps({"ok": not real, "survey": a.survey, "errors": real,
                          "warnings": [e for e in errs if e.startswith("[warn]")]}, indent=2))
    else:
        for e in errs:
            print(("  - " + e))
        print(f"{'❌ ' + str(len(real)) + ' problem(s)' if real else '✅ valid'} — {a.survey}")
    _record(a.survey, lambda r, sdir: r.record_validation(_survey_id(spec), ok=not real, sdir=sdir))
    return 1 if real else 0


def _cmd_simulate(a):
    """Coverage gate: run N synthetic respondents through every path (in-process — no subprocess) and
    record the result in the survey registry (`survey-agent list` shows tested/incidence/LOI)."""
    from survey_agent import simulate
    spec = json.load(open(a.survey))
    cov = simulate.coverage(spec, n=a.n, quota_override=(a.quota or None))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(cov, f, indent=2)
    if getattr(a, "json", False):
        print(json.dumps({"ok": not cov["problems"], **cov}, indent=2))
    else:
        print(simulate.format_report(cov))
        if a.out:
            print(f"\nwrote {a.out}")
        print(f"\nReview the run: survey-agent report {a.survey} --simulate {a.n} --open"
              f"  ·  survey-agent export {a.survey} --simulate {a.n} --format csv")
        print(simulate.gate_verdict(cov))
    _record(a.survey, lambda r, sdir: r.record_test(_survey_id(spec), cov, sdir=sdir))
    return 1 if cov["problems"] else 0


def _short(v, n=22):
    s = ", ".join(map(str, v)) if isinstance(v, list) else ("…" if isinstance(v, dict) else str(v))
    return s if len(s) <= n else s[:n - 1] + "…"


def _traits_to_audience(trait_args):
    """`--trait usage=never --trait age=70` -> a one-segment audience {segments:[{weight:1, traits:{…}}]}."""
    traits = {}
    for t in trait_args or []:
        k, _, v = t.partition("=")
        if k.strip():
            traits[k.strip()] = v.strip()
    return {"segments": [{"weight": 1, "traits": traits}]} if traits else None


def _cmd_pretest(a):
    """Persona pre-test: drive synthetic respondents through the survey to preview incidence + who
    screens out *before* fielding to real people. Declare the population with --audience <file> or
    --trait k=v (else the model invents personas). Personas come from the SURVEY_PERSONAS source
    (default `synthetic`; a real-data plugin makes the test realistic). Engine stays authoritative;
    answers are validated. TESTING only — not a substitute for real respondents."""
    from survey_agent import simulate
    spec = json.load(open(a.survey or EXAMPLE))
    audience = a.audience or _traits_to_audience(a.trait)
    _llm_hint()
    res = simulate.persona_pretest(spec, n=a.n, personas=(a.persona or None), audience=audience)
    src = res["source"] + (" · audience" if res["audience"] else "")
    print(f"\nPersona pre-test of '{res['id']}' — {res['n']} synthetic respondents "
          f"(personas: {src}; LLM: {res['llm']})\n")
    for i, r in enumerate(res["results"], 1):
        tag = ("✅ COMPLETED" if r["status"] == "completed"
               else f"⛔ {r['status'].upper()}" + (f" · {r['reason_code']}" if r.get("reason_code") else ""))
        ans = ", ".join(f"{k}={_short(v)}" for k, v in list(r["answers"].items())[:6])
        label = r["persona"] or "; ".join(f"{k}={v}" for k, v in (r.get("traits") or {}).items()) or "respondent"
        print(f"  {i}. {label}")
        print(f"     {tag}   [{ans}]")
    print(f"\nIncidence (qualified): {res['statuses'].get('completed', 0)}/{res['n']}  "
          f"({round(100 * res['incidence'])}%)")
    if res["reasons"]:
        print("Screen-outs: " + ", ".join(f"{k}×{v}" for k, v in res["reasons"].items()))
    print("\n(synthetic respondents — for testing logic/UX/incidence, not a real sample)")

    if a.out:                                   # save the full run for review
        json.dump(res, open(a.out, "w"), indent=2)
        print(f"wrote {a.out}")
    if a.report or a.open:                       # an HTML report OF THIS persona run (funnel/charts)
        import webbrowser
        from survey_agent import report as report_mod
        from survey_agent.engine.runtime import SurveyRuntime
        rt = SurveyRuntime(spec)
        rows = [{"status": report_mod._RAW_TO_BUCKET.get(r["status"], "partial"),
                 "reason_code": r.get("reason_code"), "answers": r.get("answers", {}),
                 "derived": {}, "group": r.get("segment"), "loi": None} for r in res["results"]]
        html = report_mod.render_html(report_mod.build_report(rt, rows, {}))
        out = a.report if isinstance(a.report, str) else _default_out(f"{res['id']}-pretest-report.html")
        open(out, "w").write(html)
        print(f"wrote {out}")
        if a.open:
            try:
                webbrowser.open("file://" + os.path.abspath(out))
            except Exception:
                pass
    return 0


def _cmd_compile(a):
    from survey_agent.engine.compile_md import compile_markdown
    out = json.dumps(compile_markdown(open(a.survey).read()), indent=2)
    if a.write:
        outp = a.survey[:-3] + ".survey.json" if a.survey.endswith(".md") else a.survey + ".json"
        open(outp, "w").write(out + "\n")
        print("wrote", outp)
    else:
        print(out)
    return 0


def _cmd_render(a):
    """Reverse of `compile`: a survey JSON → a readable, logic-annotated Markdown questionnaire
    (frontmatter-canonical, so it round-trips back via `compile`). `--write` saves `<stem>.md`."""
    from survey_agent.engine.compile_md import render_markdown
    md = render_markdown(json.load(open(a.survey)))
    if a.write:
        if a.survey.endswith(".survey.json"):
            outp = a.survey[: -len(".survey.json")] + ".md"
        elif a.survey.endswith(".json"):
            outp = a.survey[:-5] + ".md"
        else:
            outp = a.survey + ".md"
        open(outp, "w").write(md)
        print("wrote", outp)
    else:
        print(md)
    return 0


def _load_operator_config():
    """Read survey-agent.config.yaml (operator config — the pure engine NEVER reads this; only the CLI
    does). Looked up via $SURVEY_CONFIG, else ./survey-agent.config.yaml. Returns {} if absent/unreadable."""
    from survey_agent import config
    return config.load_config()


def _gate(approve, what):
    """Apply the launch gate (survey_agent.config.may_mark_live) for a local CLI action that would mark
    a survey live. Prints why and returns False when refused."""
    from survey_agent import config
    ok, why = config.may_mark_live(approve, surface="cli")
    if not ok:
        print(f"🔒 {what}: {why}. Pass --approve <token>.")
    return ok


def _output_dir():
    """Default directory for generated artifacts (reports, dashboards, exports) — so they don't land in
    the repo root. $SURVEY_OUTPUT_DIR, else `output.dir` in survey-agent.config.yaml, else ./reports."""
    return (os.environ.get("SURVEY_OUTPUT_DIR")
            or (_load_operator_config().get("output") or {}).get("dir")
            or "reports")


def _default_out(filename):
    """Resolve a default output path inside the configured output dir (created on demand). Used only
    when the operator didn't pass an explicit --out."""
    d = _output_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def _deploy_command():
    """The operator's publish command, or None. $SURVEY_DEPLOY_COMMAND wins, else deploy.command in
    survey-agent.config.yaml. Kept out of the engine on purpose: publishing is host-specific (git push,
    a Render deploy-hook curl, `gcloud run deploy`, …) and is the operator's to configure."""
    cmd = (os.environ.get("SURVEY_DEPLOY_COMMAND") or "").strip()
    if cmd:
        return cmd
    return ((_load_operator_config().get("deploy") or {}).get("command") or "").strip() or None


def _git_publish_hint(survey_path):
    """If `survey_path` lives in a git repo, return the exact command to publish it (commit+push, or
    push), else None. Fully defensive — returns None if git is missing or this isn't a repo."""
    import subprocess
    d = os.path.dirname(os.path.abspath(survey_path)) or "."

    def git(*args):
        return subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=5)

    try:
        if git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return None
        top = git("rev-parse", "--show-toplevel").stdout.strip() or d
        rel = os.path.relpath(os.path.abspath(survey_path), top)
        if git("status", "--porcelain", "--", os.path.abspath(survey_path)).stdout.strip():
            slug = os.path.basename(rel).split(".survey.json")[0].split(".")[0]
            return f'git add {rel} && git commit -m "launch {slug}" && git push'
        ahead = git("rev-list", "--count", "@{u}..HEAD")     # committed — ahead of upstream?
        if ahead.returncode != 0:
            return "git push -u origin HEAD"                 # no upstream configured yet
        return "git push" if (ahead.stdout.strip() or "0") != "0" else None
    except Exception:
        return None


def _print_publish_gap(sid, survey_path):
    """Make the launch≠publish gap impossible to miss after a local 'mark live'."""
    print("\n⚠️  Marked live LOCALLY only — this did NOT publish the survey. Respondents can't reach it\n"
          "    until your hosted service is deployed with this file.")
    hint = _git_publish_hint(survey_path)
    print(f"    Publish it now:  {hint}" if hint
          else "    Run your deploy step (e.g. `git push`, or your host's deploy hook) to publish.")
    if not _deploy_command():
        print("    One-shot option: set `deploy.command` in survey-agent.config.yaml (or "
              "$SURVEY_DEPLOY_COMMAND), then `survey-agent launch --deploy`.")


def _run_deploy(sid):
    """Run the configured deploy hook to actually publish. Errors helpfully if none is configured."""
    cmd = _deploy_command()
    if not cmd:
        print("\n❌ --deploy needs a deploy command, but none is configured. Add one to "
              "survey-agent.config.yaml:\n\n"
              "      deploy:\n"
              '        command: "git push origin main"   # or a Render deploy-hook curl, gcloud run deploy, …\n\n'
              "    (or set $SURVEY_DEPLOY_COMMAND). The drop-in URL is already printed above and the "
              "survey is marked live; only publishing was skipped.")
        return 1
    import subprocess
    print(f"\n🚀 Publishing '{sid}' via deploy hook:  {cmd}\n")
    try:
        rc = subprocess.run(cmd, shell=True, timeout=600).returncode      # operator-authored (trusted) command
    except Exception as e:
        print(f"\n❌ deploy command failed to run: {e}  ('{sid}' is marked live but not published.)")
        return 1
    if rc != 0:
        print(f"\n❌ deploy hook exited {rc} — '{sid}' is marked live but may NOT be published. "
              "Fix the deploy and re-run, or publish manually.")
        return 1
    print(f"\n✅ Published — deploy hook succeeded. '{sid}' will be reachable once the deploy finishes.")
    return 0


def _cmd_launch(a):
    """Produce the drop-in `survey_url` for the operator to paste into their own system (panel / CRM /
    email link), and mark the survey live. The engine never contacts anyone — this just emits the URL
    and records state; distribution + payment are the operator's system's job. Marking live is LOCAL
    bookkeeping; pass --deploy to also run the operator's configured deploy hook and actually publish."""
    from survey_agent.engine import validate as v
    if a.deploy and a.no_live:
        print("❌ --deploy can't be combined with --no-live (publishing implies going live).")
        return 1
    spec = json.load(open(a.survey))
    errs = [e for e in v.validate(spec) if not e.startswith("[warn]")]
    if errs:
        print(f"❌ not launch-ready — fix {len(errs)} validation error(s) first:")
        for e in errs:
            print("  - " + e)
        return 1
    survey = spec.get("survey", spec)
    sid = survey.get("id")
    from survey_agent import hosting
    base = hosting.public_base_url(a.base)
    uvars = (survey.get("settings", {}) or {}).get("url_variables") or ["uid", "rid", "group"]
    url = f"{base}/s/{sid}?" + "&".join(f"{u}={{{u}}}" for u in uvars)
    print(f"\n🟢 survey_url for '{sid}' — drop this into your panel / CRM / email link:\n\n  {url}\n")
    print("Replace each {placeholder} with your system's value per respondent (e.g. {uid} → the\n"
          "person's id). Serve it with `survey-agent serve` (or point the base at your hosted runtime\n"
          "with --base / SURVEY_PUBLIC_BASE_URL).")
    # A localhost base is the single most expensive silent mistake here — the link reaches nobody and
    # you find out when the completes never arrive. Say so at the moment the URL is handed over.
    unreachable = hosting.unreachable_warning(base)
    if unreachable:
        print(f"\n⚠️  {unreachable}")
    else:
        print(f"\n   (base resolved from {hosting.base_url_source(a.base)})")
    from survey_agent import preflight
    print("\n" + preflight.checklist_text())
    if not a.no_live:
        # Launch gate, consistent with the MCP surface: if this deployment configures an approval
        # token, marking live requires a human to pass the matching --approve <token>. With no token
        # configured, the human running the CLI is the gate (the URL is always emitted either way).
        from survey_agent import config
        gate = config.launch_token()
        if not config.may_mark_live(a.approve, surface="cli")[0]:
            print(f"\n🔒 Not marked live — launch is gated here (SURVEY_LAUNCH_APPROVAL_TOKEN is set). "
                  f"A human passes --approve <token> to flip '{sid}' live; the survey_url above is already usable.")
            return 0
        # In a gated deployment, also require the operator to acknowledge the checklist above before
        # flipping live — a flag, or SURVEY_OPERATOR_ACK=1 for a deployment with standing sign-off.
        acked = a.ack_responsibilities or os.environ.get("SURVEY_OPERATOR_ACK", "").lower() in ("1", "true", "yes")
        if gate and not acked:
            print("\n🔒 Approved, but not marked live — confirm the operator checklist above, then re-run with "
                  "--ack-responsibilities (or set SURVEY_OPERATOR_ACK=1). The survey_url is already usable.")
            return 0
        _record(a.survey, lambda r, sdir: r.set_status(sid, status="live", url=url, sdir=sdir))
        print(f"\nMarked '{sid}' live in the registry (survey-agent status {sid} / list).")
        _snapshot_launched(a.survey, sid)
        # Marking live is local bookkeeping — it does NOT publish. Either run the configured deploy
        # hook now (--deploy) or make the gap impossible to miss.
        if a.deploy:
            return _run_deploy(sid)
        _print_publish_gap(sid, a.survey)
    return 0


def _cmd_sign_url(a):
    """Mint a signed, drop-in survey_url for ONE respondent — HMAC over the eligibility params so a
    respondent can't forge uid/rid/group. Needs SURVEY_URL_SECRET (the same secret the runtime checks
    when settings.url_signing.required is set)."""
    from survey_agent.engine import signing
    from survey_agent.engine.runtime import SurveyRuntime
    secret = os.environ.get("SURVEY_URL_SECRET")
    if not secret:
        print("❌ set SURVEY_URL_SECRET (a shared secret) first — that's what the runtime verifies against.")
        return 1
    spec = json.load(open(a.survey))
    rt = SurveyRuntime(spec)
    cfg = (rt.d.get("settings", {}).get("url_signing") or {})
    fields = cfg.get("fields") or [rt.respondent_var, rt.response_var]
    params = {}
    for kv in a.params:
        if "=" not in kv:
            print(f"❌ expected key=value, got '{kv}'"); return 1
        k, v = kv.split("=", 1)
        params[k] = v
    missing = [f for f in fields if f not in params]
    if missing:
        print(f"❌ missing signed field(s): {', '.join(missing)} — sign-url needs every field in "
              f"url_signing.fields ({', '.join(fields)}).")
        return 1
    blank = [f for f in fields if params.get(f) == ""]
    if blank:
        # the runtime parses the query without blank values, so a signed `f=` arrives as *missing* and
        # (missing != empty in the signature) could never verify — refuse rather than mint a dead URL
        print(f"❌ signed field(s) with an empty value: {', '.join(blank)} — give each a value.")
        return 1
    from survey_agent import hosting
    base = hosting.public_base_url(a.base)
    query = signing.signed_query(params, secret, fields)
    print(f"{base}/s/{rt.d['id']}?{query}")
    return 0


def _cmd_console(a):
    """Local operator console — a visual board (survey status, completes, reports, preview, templates)
    on 127.0.0.1. A different interface to the same engine actions the agent runs."""
    from survey_agent import console
    console.serve(port=a.port, dir=a.dir, open_browser=not a.no_browser,
                  start_runtime=not a.no_runtime, host=a.host)
    return 0


def _cmd_api(a):
    """Run the API gateway — the authenticated front door to the data backend (definitions + responses).
    The console / agent / CLI point at it (SURVEY_DEFINITION_SOURCE=http, SURVEY_STORE=http) instead of
    opening their own DB connection. Run it WHERE the database lives, with the real backend selected:
    SURVEY_DEFINITION_SOURCE=mongo SURVEY_STORE=mongo SURVEY_API_TOKEN=… survey-agent api --host 0.0.0.0"""
    try:
        import uvicorn
        from survey_agent.api.app import app
    except SystemExit as e:
        print(e)
        return 1
    except Exception:
        print("the API gateway needs the [api] extra: pip install 'survey-agent[api]'")
        return 1
    host = a.host or "127.0.0.1"
    backing = (f"definitions={os.environ.get('SURVEY_DEFINITION_SOURCE', 'file')} · "
               f"store={os.environ.get('SURVEY_STORE', 'memory')}")
    print(f"[api] survey-agent API gateway on http://{host}:{a.port}  ({backing})")
    if not os.environ.get("SURVEY_API_TOKEN"):
        print("      ⚠️  SURVEY_API_TOKEN is not set — running WITHOUT auth (local/dev only). Set it and")
        print("      put a proxy/OAuth in front before exposing it; clients send it as a Bearer token.")
    uvicorn.run(app, host=host, port=a.port, log_level="warning")
    return 0


def _is_loopback(host):
    return (host or "127.0.0.1") in ("127.0.0.1", "localhost", "::1")


def _cmd_serve(a):
    if a.preview and (a.public or not _is_loopback(a.host)):
        print("❌ --preview serves drafts and other not-live surveys — it is local only and can't be "
              "combined with --public or a non-loopback --host.")
        return 2
    if a.preview:
        # local sandbox: every lifecycle state is served (and collected), so a draft can be taken end to end
        os.environ["SURVEY_SERVE_STATES"] = "*"
    elif (os.environ.get("SURVEY_SERVE_STATES") or "").strip() == "*" and (a.public or not _is_loopback(a.host)):
        print("❌ SURVEY_SERVE_STATES=* (serve every state) is a local-preview setting — refusing to expose "
              "drafts with --public / a non-loopback host. Unset it, or list explicit states.")
        return 2
    if getattr(a, "multi", False):
        # multi-survey mode: serve EVERY survey in SURVEY_DIR at /s/<id> from one process (the hosted
        # "one service, many surveys" shape). Set BEFORE importing server (it reads env at import time).
        os.environ["SURVEY_MULTI"] = "1"
        if a.dir:
            os.environ["SURVEY_DIR"] = a.dir
        os.environ.setdefault("SURVEY_DIR", "surveys")
    else:
        os.environ["SURVEY_FILE"] = a.survey or os.environ.get("SURVEY_FILE", EXAMPLE)
    host = a.host
    if a.public:
        # bind all interfaces. Store + integration are whatever the operator configured (env / config
        # yaml), else the shared defaults (file store, no outbound integration) — set in main().
        host = host or "0.0.0.0"
        _llm_hint()
        print(f"public mode: binds {host} · integration={os.environ.get('SURVEY_INTEGRATION') or 'none'} "
              f"· serves live surveys only\n")
    from survey_agent import server
    if os.environ.get("SURVEY_STORE") == "file":
        print(f"responses → {os.environ.get('SURVEY_DATA_DIR', './survey_data')}/  "
              f"(pull later: survey-agent export/report, or GET /api/responses)")
    try:
        server.serve(a.port, host=host)
    except server.PortInUse:
        return 1
    return 0


def _cmd_demo(a):
    receiver_base = f"http://127.0.0.1:{a.port_receiver}"
    runtime_base = f"http://127.0.0.1:{a.port}"
    if a.survey:
        os.environ["SURVEY_FILE"] = a.survey       # demo a specific survey (e.g. one you just authored)
    os.environ.setdefault("SURVEY_PUBLIC_BASE_URL", runtime_base)
    # the demo is a local sandbox: passbacks go to its own mock receiver, and every lifecycle state is
    # served (a freshly scaffolded / authored draft is takeable without marking it live)
    os.environ["SURVEY_INTEGRATION"] = "webhook"
    os.environ["SURVEY_WEBHOOK_URL"] = f"{receiver_base}/hook"
    os.environ["SURVEY_SERVE_STATES"] = "*"
    os.environ.setdefault("SURVEY_LLM_ENRICH", "1")
    if a.quota:
        os.environ["DEMO_QUOTA_LIMIT"] = str(a.quota)
    _llm_hint()
    from http.server import ThreadingHTTPServer
    from survey_agent import server, receiver
    # bind both sockets HERE (main thread) so a busy port is one clear error, not a dead thread + a
    # demo that keeps "running" with nothing listening
    try:
        rhttpd = server.bind_http(ThreadingHTTPServer, receiver.Handler, "127.0.0.1", a.port_receiver,
                                  label="receiver")
        httpd = server.bind(a.port, host="127.0.0.1")
    except server.PortInUse:
        return 1
    print(f"[RECEIVER] mock webhook receiver on {receiver_base}/hook")
    rt_thread = threading.Thread(target=server.serve, kwargs={"httpd": httpd}, daemon=True)
    rv_thread = threading.Thread(target=rhttpd.serve_forever, daemon=True)
    rv_thread.start()
    rt_thread.start()
    time.sleep(0.3)
    print(f"\n  survey runtime : {runtime_base}/")
    print(f"  webhook receiver: {receiver_base}/  (watch this terminal for survey events)")
    print(f"  open {runtime_base}/ to start; Ctrl-C to stop.\n")
    if not a.no_browser:
        try:
            import webbrowser
            webbrowser.open(f"{runtime_base}/")
        except Exception:
            pass
    try:
        while rt_thread.is_alive() and rv_thread.is_alive():
            rt_thread.join(0.5)
    except KeyboardInterrupt:
        print("\nbye.")
        return 0
    print("❌ the demo's server stopped unexpectedly — see the error above.", file=sys.stderr)
    return 1


def _cmd_convo(a):
    from survey_agent.conversation import Conversation
    from survey_agent import enrich
    spec = json.load(open(a.survey or EXAMPLE))
    survey = spec.get("survey", spec)
    # resolve any bundled behavioral context so the "ask why" turn can appear (demo)
    enr = {}
    try:
        os.environ.setdefault("SURVEY_LLM_ENRICH", "1")
        enr = enrich.StaticProvider().resolve("1001", [s for s in survey.get("settings", {}).get("enrichments", [])
                                                       if isinstance(s, dict) and "event_type" in s])
    except Exception:
        pass
    _llm_hint()
    convo = Conversation(spec, url_vars={"uid": "1001", "rid": "1", "group": "demo"}, enrichments=enr)
    print(f"(LLM: {convo.llm.name})\n")
    print("Assistant:", convo.opening()["message"])
    scripted = iter(a.scripted or [])
    out = {}
    while True:
        if a.scripted is not None:
            user = next(scripted, None)
            if user is None:                       # script exhausted before a terminal
                where = f" at question '{out.get('question_id')}'" if out.get("question_id") else ""
                print(f"\n[script ended{where} — the survey did not reach a terminal]")
                return 1
            print("You:", user)
        else:
            try:
                user = input("You: ")
            except EOFError:                       # stdin closed (piped / Ctrl-D): leave cleanly
                print("\n[input closed — conversation ended before a terminal]")
                return 1
            except KeyboardInterrupt:
                print("\nbye.")
                return 130
        out = convo.reply(user)
        print("Assistant:", out["message"])
        if out.get("done"):
            print(f"\n[survey terminal: {out['status']}]")
            return 0


def _cmd_report(a):
    import json as _json
    import webbrowser
    from survey_agent import report as report_mod
    from survey_agent.engine.runtime import SurveyRuntime
    rt = SurveyRuntime(_json.load(open(a.survey)))
    store = None
    if not a.simulate:
        import survey_agent.store as store_pkg
        store = store_pkg.get_store()
    if a.insights:
        _llm_hint()
    html, rep = report_mod.generate(rt, store=store, simulate_n=a.simulate, seed=a.seed,
                                    with_insights=a.insights, return_report=True)
    out = a.out or _default_out(f"{rt.d['id']}-report.html")
    open(out, "w").write(html)
    src = f"simulation (n={a.simulate})" if a.simulate else f"the {os.environ.get('SURVEY_STORE') or 'memory'} store"
    print(f"wrote {out}  (from {src}{'; + AI insights' if a.insights else ''})")
    # ground truth in the terminal, so a stale browser cache can't mislead (the file:// is overwritten
    # in place; if the page looks unchanged after a re-run, hard-refresh — these numbers are current)
    print(f"  {rep['n']} responses · {rep['funnel']['counts'][0]} completed · {rep['incidence']}% incidence")
    if rep["quotas"]:
        print("  quotas: " + ", ".join(f"{q['id']} {q['count']}/{q['limit'] if q['limit'] is not None else '∞'}"
                                       for q in rep["quotas"]))
    if a.open:
        try:                                          # cache-bust the file:// URL so a re-run isn't masked
            webbrowser.open("file://" + os.path.abspath(out) + f"?v={int(time.time())}")
        except Exception:
            pass
    return 0


def _cmd_critique(a):
    """Methodology review of a survey draft — flags leading/double-barreled questions, missing
    opt-outs, overlapping ranges, jargon. Advisory (judgment, not pass/fail); sees the definition
    only. Offline = high-precision heuristics; with a key = a fuller methodologist read."""
    import json as _json
    from survey_agent import ai
    spec = _json.load(open(a.survey))
    _llm_hint()
    c = ai.get_llm().critique(spec)
    if getattr(a, "json", False):
        print(json.dumps(c, indent=2))
        return 0
    sid = (spec.get("survey", spec) if isinstance(spec, dict) else {}).get("id", "?")
    print(f"\nMethodology review of '{sid}'  ({c.get('engine', '?')})\n{c.get('summary', '')}\n")
    icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    for i in c.get("issues", []):
        print(f"  {icon.get(i.get('severity'), '•')} [{i.get('id')}] {i.get('type')}: {i.get('issue')}")
        if i.get("suggestion"):
            print(f"       → {i['suggestion']}")
    if not c.get("issues"):
        print("  ✅ nothing flagged")
    return 0


def _cmd_insights(a):
    """Print an AI analyst read-out of a survey's results (headline, findings, open-end themes,
    watch-outs, suggested follow-ups) to the console. Offline = a deterministic stub."""
    import json as _json
    from survey_agent import report as report_mod, insights as insights_mod
    from survey_agent.engine.runtime import SurveyRuntime
    rt = SurveyRuntime(_json.load(open(a.survey)))
    if a.simulate:
        rows, quotas = report_mod.collect_via_simulation(rt, a.simulate, a.seed)
    else:
        import survey_agent.store as store_pkg
        rows, quotas = report_mod.collect_from_store(rt, store_pkg.get_store())
    _llm_hint()
    report = report_mod.build_report(rt, rows, quotas)
    print(insights_mod.to_text(insights_mod.generate_insights(rt, rows, report)))
    return 0


def _cmd_export(a):
    import json as _json
    from survey_agent import exporting
    from survey_agent.engine.runtime import SurveyRuntime
    rt = SurveyRuntime(_json.load(open(a.survey)))
    store = None
    if not a.simulate:
        import survey_agent.store as store_pkg
        store = store_pkg.get_store()
    responses = exporting.collect_responses(rt, store=store, simulate_n=a.simulate, seed=a.seed)
    cols, rows = exporting.flatten(rt, responses)
    fmt = a.format or ("parquet" if (a.out or "").endswith(".parquet") else "csv")
    out = a.out or _default_out(f"{rt.d['id']}-responses.{'parquet' if fmt == 'parquet' else 'csv'}")
    if fmt == "parquet":
        exporting.to_parquet(cols, rows, out)
    else:
        with open(out, "w", newline="") as f:
            f.write(exporting.to_csv(cols, rows))
    print(f"wrote {out}  ({len(rows)} rows, {len(cols)} columns)")
    return 0


INIT_CONFIG = """\
# survey-agent operator config. Environment variables (and .env) override every key here.
# Folder holding your *.survey.json files (env: SURVEY_DIR).
surveys:
  path: ./surveys
# Where responses are stored: memory | file | sqlite | mongo | http | your.pkg:Store (env: SURVEY_STORE).
store:
  backend: file
# Passback delivery when a respondent finishes: none | redirect | webhook | panel | your plugin
# (env: SURVEY_INTEGRATION). `survey-agent demo` always uses its own mock webhook receiver.
integration: none
# Default theme for surveys that don't set settings.theme (env: SURVEY_THEME).
theme: paper
# Where generated reports / dashboards / exports are written (env: SURVEY_OUTPUT_DIR).
output:
  dir: ./reports
# Publish hook run by `survey-agent launch --deploy` (host-specific; leave blank to skip),
# e.g. "git push origin main", a Render deploy-hook curl, or "gcloud run deploy ...".
deploy:
  command: ""
"""

# Comments live on their OWN lines: some .env loaders keep an inline `# …` as part of the value.
INIT_DOTENV = """\
# --- Bring your own model (powers `survey-agent author`, conversational mode, LLM analysis) ---
# Set ONE of these. `survey-agent author` and the agentic features auto-detect whichever is present.
# Everything else (validate / simulate / demo) runs fully offline with no key.
# Claude (get one at console.anthropic.com):
ANTHROPIC_API_KEY=
# GPT / Codex (get one at platform.openai.com):
OPENAI_API_KEY=
# Provider: auto (default) | anthropic | openai | stub
# SURVEY_LLM=auto
# Override the model id (else a sensible default per provider):
# SURVEY_LLM_MODEL=

# --- Runtime (these override survey-agent.config.yaml) ---
# Where responses are stored: memory | file | sqlite | mongo | http | your.pkg:Store
# SURVEY_STORE=file
# Where a completed respondent is sent when a survey's redirect kind is 'default':
SURVEY_DEFAULT_REDIRECT_URL=
# Default folder for generated reports / dashboards / exports (else ./reports):
# SURVEY_OUTPUT_DIR=reports
"""


def _cmd_init(a):
    dst = a.dir
    os.makedirs(os.path.join(dst, "surveys"), exist_ok=True)
    example_dst = os.path.join(dst, "surveys", "example.survey.json")
    if not os.path.exists(example_dst):          # never clobber an existing example (safe in a populated dir)
        shutil.copy(EXAMPLE, example_dst)
    cfg = os.path.join(dst, "survey-agent.config.yaml")
    if not os.path.exists(cfg):
        open(cfg, "w").write(INIT_CONFIG)
    envf = os.path.join(dst, ".env")
    if not os.path.exists(envf):
        open(envf, "w").write(INIT_DOTENV)
    gi = os.path.join(dst, ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w").write(".env\nsurvey_data/\nreports/\n__pycache__/\n")
    abs_dst = os.path.abspath(dst)
    here = abs_dst == os.path.abspath(os.getcwd())
    print(f"scaffolded {abs_dst}{'  (current folder)' if here else ''}")
    print("  surveys/example.survey.json · survey-agent.config.yaml · .env")
    print("  (responses will land in survey_data/ here once you run serve/demo)")
    if not here:                                 # only nudge a cd when we scaffolded somewhere else
        print(f"\nstep into it first:\n  cd {abs_dst}")
    print("\nnext — the agentic way (add a key to .env first):")
    print('  survey-agent author "screen for <audience>, route qualifiers to <task>"')
    print("\nor kick the tires (no key needed):")
    print("  survey-agent selftest      # confirm the install validates + simulates")
    print("  survey-agent demo          # clickable form + AI-conversation demo (local sandbox)")
    return 0


_STATE_ICON = {"draft": "✏️ ", "validated": "🔎", "tested": "✅", "tested(failing)": "⚠️ ",
               "approved": "👍", "live": "🟢", "paused": "⏸️ ", "closed": "⏹️ "}


def _cmd_list(a):
    """The survey-management view: every survey in the project with its purpose and lifecycle state
    (draft → validated → tested → live), last test incidence/LOI, tags, and any live URL. Reads the
    flat `*.survey.json` files + the `.registry.json` sidecar. `--json` for the machine-readable
    catalog (also the MCP `list_surveys` tool)."""
    from survey_agent import registry
    cat = registry.catalog(a.dir)
    sdir = registry.surveys_dir(a.dir)
    if a.json:
        print(json.dumps(cat, indent=2))
        return 0
    if not cat:
        print(f'No surveys in {sdir}/  —  create one:  survey-agent author "<brief>"')
        return 0
    print(f"\n{len(cat)} survey(s) in {sdir}/\n")
    for e in cat:
        t = e.get("tested") or {}
        meta = []
        if t.get("incidence") is not None:
            meta.append(f"inc {round(100 * t['incidence'])}%")
        if t.get("loi_min"):
            meta.append(f"{t['loi_min']}m")
        if (e.get("live") or {}).get("url"):
            meta.append("url↗")
        if e.get("tags"):
            meta.append("#" + ",".join(e["tags"]))
        if e.get("missing_file"):
            meta.append("(file missing)")
        tail = ("   " + " · ".join(meta)) if meta else ""
        print(f"  {_STATE_ICON.get(e['state'], '  ')} {e['id']:26} {e['state']:16}{tail}")
        desc = e.get("description") or e.get("title")
        if desc:
            print(f"        {_short(desc, 74)}")
    print("\n  detail:  survey-agent status <id>     ·     set state:  survey-agent status <id> --set live --url <survey_url>")
    return 0


def _cmd_status(a):
    """Show one survey's full lifecycle record, or set it: `--set <state>` (draft|validated|tested|
    approved|live|paused|closed), `--note "<text>"`, `--url <survey_url>` (record where it's live).
    Writes through the configured definition store — so with a runtime backend (mongo) a pause/close
    takes effect with no redeploy."""
    from survey_agent import registry, definitions
    store = definitions.get_definition_store(dir=a.dir)
    if a.set or a.note is not None or a.url is not None or a.project is not None:
        if a.set and a.set not in registry.STATES:
            print(f"unknown state '{a.set}' — choose one of: {', '.join(registry.STATES)}")
            return 1
        if a.set == "live" and not _gate(a.approve, f"'{a.survey_id}' not marked live"):
            return 1
        fields = {}
        if a.set is not None:
            fields["status"] = a.set
        if a.set == "live" or a.url is not None:
            live = {"at": registry.now()} if a.set == "live" else {}
            if a.url is not None:
                live["url"] = a.url
            fields["live"] = live
        if a.note is not None:
            fields["note"] = a.note
        if a.project is not None:
            fields["project"] = a.project.strip() or None      # move to a project ("" → ungrouped)
        store.set_registry(a.survey_id, **fields)
        print(f"updated {a.survey_id}")
    entry = next((e for e in store.list_surveys() if e["id"] == a.survey_id), None)
    if not entry:
        print(f"no survey '{a.survey_id}' (try: survey-agent list)")
        return 1
    reg = (store.get_registry(a.survey_id) or {}) if hasattr(store, "get_registry") else {}
    for k in ("live_version", "latest_version"):        # what's served vs. what's saved
        if reg.get(k) is not None:
            entry[k] = reg[k]
    print(json.dumps(entry, indent=2))
    return 0


def _cmd_publish(a):
    """Publish a survey definition to the configured store and mark it live: validate → store (mongo
    auto-bumps the version on a content change; file writes the file) → set live. With the `file`
    backend you still run your deploy step (git push) to reach a hosted runtime; with a runtime backend
    (mongo) it's live immediately, no redeploy. The local file stays the authored artifact + backup."""
    from survey_agent.engine import validate as v
    from survey_agent import definitions
    spec = json.load(open(a.survey))
    errs = [e for e in v.validate(spec) if not e.startswith("[warn]")]
    if errs:
        for e in errs:
            print("  - " + e)
        print(f"❌ not published — fix {len(errs)} problem(s) first: {a.survey}")
        return 1
    store = definitions.get_definition_store(dir=a.dir)
    sid = spec["survey"]["id"]
    from survey_agent import config
    allowed, why = config.may_mark_live(a.approve, surface="cli")
    version = store.put_definition(spec)
    if not allowed:
        print(f"🔒 stored {sid} v{version}, but NOT marked live: {why}. Re-run with --approve <token>.")
        return 1
    store.set_registry(sid, status="live", live_version=version)
    if spec["survey"].get("version") != version:          # mirror the assigned version into the file
        spec["survey"]["version"] = version
        with open(a.survey, "w") as f:
            json.dump(spec, f, indent=2)
            f.write("\n")
    kind = (os.environ.get("SURVEY_DEFINITION_SOURCE") or "file").lower()
    print(f"✅ published {sid} v{version} → {kind} store")
    if kind == "file":
        print("   file backend: run your deploy step (git push / deploy hook) to reach the hosted runtime.")
    else:
        print("   live now — no redeploy. Commit the local file for backup.")
    return 0


def _cmd_defs_export(a):
    """Dump definitions from the configured store to local files (cold backup / readable mirror).
    Default: the live version per survey → <out>/<id>.survey.json. `--all`: every version too →
    <out>/<id>.v<n>.survey.json."""
    from survey_agent import definitions
    store = definitions.get_definition_store()
    out = a.out or "surveys"
    os.makedirs(out, exist_ok=True)
    n = 0
    for e in store.list_surveys():
        if e.get("missing_file"):
            continue
        sid = e["id"]
        live = store.get_definition(sid)
        if live is not None:
            _write_json(os.path.join(out, f"{sid}.survey.json"), live); n += 1
        if a.all:
            for ver, _, _ in store.iter_versions(sid):
                spec = store.get_definition(sid, ver)
                if spec is not None:
                    _write_json(os.path.join(out, f"{sid}.v{ver}.survey.json"), spec)
    print(f"exported {n} live definition(s) → {out}/" + (" (+ every version)" if a.all else ""))
    return 0


def _cmd_defs_import(a):
    """Rebuild the configured store from local files: publish each <id>.survey.json (live). The DB-loss
    recovery path — files are the durable backup."""
    import glob
    import re as _re
    from survey_agent import definitions
    from survey_agent import config
    store = definitions.get_definition_store()
    src = a.dir or os.environ.get("SURVEY_DIR") or "surveys"
    allowed, why = config.may_mark_live(a.approve, surface="cli")
    n = 0
    for path in sorted(glob.glob(os.path.join(src, "*.survey.json"))):
        if _re.search(r"\.v\d+\.survey\.json$", path):       # skip versioned dumps; import live files only
            continue
        spec = json.load(open(path))
        sid = spec["survey"]["id"]
        ver = store.put_definition(spec)
        if allowed:
            store.set_registry(sid, status="live", live_version=ver)
        print(f"  imported {sid} v{ver}"); n += 1
    print(f"imported {n} definition(s) into the store")
    if not allowed:
        print(f"🔒 definitions stored but NOT marked live: {why}. Re-run with --approve <token>.")
    return 0


def _cmd_defs_diff(a):
    """Reconcile local files vs the configured store's live versions; flag drift (file added, content
    differs, or store-only). Exit 1 if anything differs."""
    import glob
    import re as _re
    from survey_agent import definitions
    from survey_agent.definitions.mongo import _hash
    store = definitions.get_definition_store()
    src = a.dir or os.environ.get("SURVEY_DIR") or "surveys"
    local = {}
    for path in sorted(glob.glob(os.path.join(src, "*.survey.json"))):
        if _re.search(r"\.v\d+\.survey\.json$", path):
            continue
        spec = json.load(open(path)); local[spec["survey"]["id"]] = spec
    store_ids = {e["id"] for e in store.list_surveys() if not e.get("missing_file")}
    drift = 0
    for sid, spec in local.items():
        live = store.get_definition(sid)
        if live is None:
            print(f"  + {sid}: in files, not in store"); drift += 1
        elif _hash(live) != _hash(spec):
            print(f"  ~ {sid}: local file differs from store live version"); drift += 1
    for sid in sorted(store_ids - set(local)):
        print(f"  - {sid}: in store, no local file"); drift += 1
    print(f"{'⚠ ' + str(drift) + ' difference(s)' if drift else '✅ in sync'}")
    return 1 if drift else 0


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def _cmd_dashboard(a):
    """Write a self-contained HTML management dashboard: every survey + purpose + lifecycle state,
    deep-linking into each survey's HTML report. `--live` annotates each card with the drop-in URL,
    accepted params, and live counts (completed/incidence/quota) from the store; `--reports` also
    writes each survey's full report and links it; `--simulate N` uses N synthetic respondents. No
    server needed. For live counts off a hosted Mongo, set SURVEY_STORE/SURVEY_MONGODB_URI first."""
    from survey_agent import registry, dashboard
    cat = registry.catalog(a.dir)
    sdir = registry.surveys_dir(a.dir)
    out = a.out or _default_out("dashboard.html")
    outdir = os.path.dirname(os.path.abspath(out))
    report_links = {}
    live = {}
    from survey_agent import hosting
    base = hosting.public_base_url(a.base_url, default="") if (
        a.base_url or os.environ.get("SURVEY_PUBLIC_BASE_URL") or hosting.detect_host()[1]) else ""
    if a.reports or a.simulate or a.live:
        from survey_agent import report as report_mod
        from survey_agent.engine.runtime import SurveyRuntime
        store = None
        if not a.simulate:
            import survey_agent.store as store_pkg
            store = store_pkg.get_store()
        for e in cat:
            if not e.get("file"):
                continue
            try:
                rt = SurveyRuntime(json.load(open(os.path.join(sdir, e["file"]))))
                info = {"params": [rt.respondent_var, rt.response_var]}
                if base:
                    info["url"] = f"{base}/s/{e['id']}"
                elif (e.get("live") or {}).get("url"):
                    info["url"] = e["live"]["url"]
                if a.reports or a.simulate:                     # full report file + link, and counts
                    html, rep = report_mod.generate(rt, store=store, simulate_n=a.simulate,
                                                    seed=7, return_report=True)
                    rp = f"{e['id']}-report.html"
                    with open(os.path.join(outdir, rp), "w") as f:
                        f.write(html)
                    report_links[e["id"]] = rp
                else:                                           # --live: counts from the store, no files
                    rows, quotas = report_mod.collect_from_store(rt, store)
                    rep = report_mod.build_report(rt, rows, quotas)
                info["n"] = rep["n"]
                info["completed"] = rep["funnel"]["counts"][0]
                info["incidence"] = rep["incidence"]
                info["loi_median"] = (rep.get("loi") or {}).get("median_min")   # minutes (dashboard shows "m")
                info["quotas"] = rep.get("quotas") or []
                live[e["id"]] = info
            except Exception:
                pass
    sub = None
    if live:
        src = f"simulation (n={a.simulate})" if a.simulate else f"the {os.environ.get('SURVEY_STORE') or 'memory'} store"
        sub = f"{len(cat)} survey(s) · live counts from {src}"
    with open(out, "w") as f:
        f.write(dashboard.render_html(cat, report_links=report_links, live=live, subtitle=sub))
    print(f"wrote {out}  ({len(cat)} surveys"
          + (f", {len(report_links)} reports" if report_links else "")
          + (f", live counts for {len(live)}" if live and not report_links else "")
          + ")")
    if a.open:
        try:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(out) + f"?v={int(time.time())}")
        except Exception:
            pass
    return 0


def _cmd_templates(a):
    """List available templates across all layers (core / plugin / overlay)."""
    from survey_agent import templates as T
    ts = T.list_templates(type=a.type)
    if a.json:
        keys = ("id", "title", "type", "source", "description", "tags", "params")
        print(json.dumps([{k: t.get(k) for k in keys} for t in ts], indent=2))
        return 0
    if not ts:
        print("no templates found  (core ships several; add your own via $SURVEY_TEMPLATES_DIR or ./templates)")
        return 0
    icon = {"core": "·", "plugin": "▸", "overlay": "+"}
    print(f"\n{len(ts)} template(s)   scaffold with:  survey-agent new <id> --slug <slug> --title \"…\"\n")
    for t in ts:
        params = ", ".join(p.get("name", "") + ("*" if p.get("required") else "") for p in t.get("params", []))
        print(f"  {icon.get(t['source'], ' ')} {t['id']:<22} [{t['type']}]  ({t['source']})")
        if t.get("description"):
            print(f"      {t['description']}")
        if params:
            print(f"      params: {params}")
    print("\n  legend:  · core   ▸ plugin   + overlay      (* = required param)")
    return 0


def _slug_ok(s):
    return bool(s) and re.match(r"^[a-zA-Z0-9_-]+$", s)


def _cmd_new(a):
    """Scaffold a new survey from a template: substitute params, write, validate, render, (optional) sim."""
    from survey_agent import templates as T, registry
    from survey_agent.engine import validate as v
    t = T.get(a.template)
    if not t:
        avail = ", ".join(sorted(x["id"] for x in T.list_templates())) or "(none)"
        print(f"❌ unknown template {a.template!r}\n   available: {avail}\n   list them with: survey-agent templates")
        return 2
    if not _slug_ok(a.slug):
        print(f"❌ invalid --slug {a.slug!r} — use letters, digits, '-' or '_' only.")
        return 2
    params = {"slug": a.slug, "title": a.title or a.slug.replace("-", " ").replace("_", " ").strip().title()}
    for kv in (a.param or []):
        k, _, val = kv.partition("=")
        if k.strip():
            params[k.strip()] = val
    missing = T.missing_params(t, params)
    if missing:
        print(f"❌ missing required param(s): {', '.join(missing)}\n   pass them with --param name=value")
        return 2

    try:
        spec = T.render(t, params)
    except ValueError as e:
        print(f"❌ {e}")
        return 2
    out_dir = a.out or registry.surveys_dir()
    os.makedirs(out_dir, exist_ok=True)
    outp = os.path.join(out_dir, f"{a.slug}.survey.json")
    if os.path.exists(outp) and not a.force:
        print(f"❌ {outp} already exists — pass --force to overwrite, or choose another --slug.")
        return 2
    with open(outp, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")
    print(f"✅ scaffolded {outp}   (template '{t['id']}', source: {t['source']})")

    errs = v.validate(spec)
    real = [e for e in errs if not e.startswith("[warn]")]
    for e in errs:
        print("  - " + e)
    print("   " + (f"❌ {len(real)} problem(s)" if real else "✅ valid"))
    _record(outp, lambda r, sdir: r.record_validation(a.slug, ok=not real, sdir=sdir))

    try:
        from survey_agent.engine.compile_md import render_markdown
        mdp = outp[: -len(".survey.json")] + ".md"
        open(mdp, "w").write(render_markdown(spec))
        print(f"   wrote {mdp}")
    except Exception:
        pass

    if a.simulate:
        from survey_agent import simulate
        cov = simulate.coverage(spec, n=a.simulate)
        print("\n" + simulate.format_report(cov))
        print(simulate.gate_verdict(cov))
        _record(outp, lambda r, sdir: r.record_test(a.slug, cov, sdir=sdir))

    print(f"\nnext:  edit {outp}  ·  survey-agent validate {outp}  ·  survey-agent serve --survey {outp}")
    return 1 if real else 0


def _cmd_copy(a):
    """Duplicate an existing survey into a new one — same content, fresh id/title/version and a clean
    lifecycle. The third way to START a survey (alongside `author` from a brief and `new` from a
    template): "I need this exact survey again, maybe with a tweak." Writes <surveys-dir>/<slug>.survey.json
    (+ readable .md), validates, and registers it as a fresh draft (it does NOT inherit the source's
    validated/tested/live state). Internal page/question/action ids are kept — only the survey id changes."""
    from survey_agent import registry
    from survey_agent.engine import validate as v
    src = _resolve_survey(a.source)
    if not src:
        print(f"❌ can't find source survey {a.source!r} — pass a slug (a survey in the surveys dir) "
              f"or a path to a .survey.json file.\n   list what's available: survey-agent list")
        return 2
    if not _slug_ok(a.slug):
        print(f"❌ invalid --slug {a.slug!r} — use letters, digits, '-' or '_' only.")
        return 2

    spec = json.load(open(src))
    s = spec.get("survey", spec)
    s["id"] = a.slug
    if a.title:
        s["title"] = a.title
    s["version"] = 1                       # a copy starts its own version lineage at 1, not the donor's

    out_dir = a.out or registry.surveys_dir()
    os.makedirs(out_dir, exist_ok=True)
    outp = os.path.join(out_dir, f"{a.slug}.survey.json")
    if os.path.abspath(outp) == os.path.abspath(src):
        print(f"❌ source and destination are the same file ({outp}) — choose a different --slug.")
        return 2
    if os.path.exists(outp) and not a.force:
        print(f"❌ {outp} already exists — pass --force to overwrite, or choose another --slug.")
        return 2
    with open(outp, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")
    print(f"✅ copied {src}  →  {outp}   (id '{a.slug}', version 1, fresh draft)")

    errs = v.validate(spec)
    real = [e for e in errs if not e.startswith("[warn]")]
    for e in errs:
        print("  - " + e)
    print("   " + (f"❌ {len(real)} problem(s)" if real else "✅ valid"))
    _record(outp, lambda r, sdir: r.record_validation(a.slug, ok=not real, sdir=sdir))

    try:
        from survey_agent.engine.compile_md import render_markdown
        mdp = outp[: -len(".survey.json")] + ".md"
        open(mdp, "w").write(render_markdown(spec))
        print(f"   wrote {mdp}")
    except Exception:
        pass

    print(f"\nnext:  edit {outp}  ·  survey-agent validate {outp}  ·  survey-agent serve --survey {outp}")
    return 1 if real else 0


def _cmd_templatize(a):
    """Promote an existing survey into a reusable TEMPLATE so it shows up in `survey-agent templates`
    and can be stamped out with `survey-agent new`. The mechanical inverse of `new`: tokenize id/title
    to ${slug}/${title}, lift theme + url_variables into a generated template.yaml, write the pair under
    <dest>/<id>/, and round-trip-verify it scaffolds back to a valid survey. Any OTHER knob you want
    (a service/brand name, an image URL) is a one-line hand-edit to the manifest afterward — this does
    the deterministic 80% and leaves the judgment 20% to you."""
    import yaml
    from survey_agent import templates as T
    from survey_agent.engine import validate as v
    src = _resolve_survey(a.source)
    if not src:
        print(f"❌ can't find source survey {a.source!r} — pass a slug (a survey in the surveys dir) "
              f"or a path to a .survey.json file.\n   list what's available: survey-agent list")
        return 2
    stem = os.path.basename(src).split(".survey.json")[0].split(".")[0]
    # default template id: the filename stem — or the survey id, when that's how the source was named
    tid = a.id or (a.source if (not os.path.isfile(a.source) and _slug_ok(a.source)) else stem)
    if not _slug_ok(tid):
        print(f"❌ invalid template --id {tid!r} — use letters, digits, '-' or '_' only.")
        return 2

    spec = json.load(open(src))
    manifest, spec = T.from_survey(spec, id=tid, title=a.title, type=a.type)

    dest = a.out or "templates"             # in-repo + committed by default; --out overlay/templates for private/brand
    tdir = os.path.join(dest, tid)
    if os.path.isdir(tdir) and not a.force:
        print(f"❌ {tdir} already exists — pass --force to overwrite, or choose another --id.")
        return 2
    os.makedirs(tdir, exist_ok=True)
    man_path = os.path.join(tdir, "template.yaml")
    spec_path = os.path.join(tdir, "survey.json")
    with open(man_path, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, default_flow_style=False)
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")
    print(f"✅ templatized {src}  →  {tdir}/   (template id '{tid}')")

    # round-trip-verify: render the freshly written template + validate, so we never leave a broken one
    try:
        rendered = T.verify_dir(tid, manifest["params"], manifest.get("requires", {}), spec_path)
        real = [e for e in v.validate(rendered) if not e.startswith("[warn]")]
    except ValueError as e:
        real = [str(e)]
    for e in real:
        print("  - " + e)
    print("   " + (f"❌ scaffold does NOT validate ({len(real)} problem(s)) — the source survey may be "
                   f"invalid; fix it and re-run" if real else "✅ round-trips to a valid survey"))

    # is `dest` on the template search path (so it shows up in `survey-agent templates`)?
    norm = os.path.normpath(dest)
    env_dirs = [os.path.normpath(d) for d in (os.environ.get("SURVEY_TEMPLATES_DIR") or "").split(os.pathsep) if d]
    discoverable = norm in (os.path.join("overlay", "templates"), "templates") or norm in env_dirs
    if discoverable:
        print(f"\n   appears in `survey-agent templates` (source: overlay).  Add more ${{params}} to "
              f"{man_path}\n   for any other knobs (e.g. a brand/service name or image URL), each with a "
              f"default so it stays scaffoldable.")
    else:
        print(f"\n   NOTE: {dest} isn't on the template search path — for it to appear in "
              f"`survey-agent templates`,\n   use --out templates (in-repo) or --out overlay/templates "
              f"(private), or set $SURVEY_TEMPLATES_DIR={dest}.")
    print(f"\nnext:  survey-agent templates  ·  survey-agent new {tid} --slug <new-slug> --title \"…\"")
    return 1 if real else 0


def _editable_mismatch():
    """Detect the multi-clone editable-install trap: the current folder is itself a survey_agent source
    checkout, but the RUNNING `survey_agent` resolves to a *different* checkout (classic cause: `pip
    install -e .` was run in clone A, then you cloned B and work there without re-installing — a venv
    allows only one editable install per package, so the CLI keeps running A's code). Returns
    (running_pkg_dir, this_repo_root) on a mismatch, else None. Pure, cheap path checks — no git."""
    try:
        import survey_agent
        running = os.path.realpath(os.path.dirname(survey_agent.__file__))
        here_pkg = os.path.realpath(os.path.join(os.getcwd(), "src", "survey_agent"))
        if os.path.isfile(os.path.join(here_pkg, "__init__.py")) and here_pkg != running:
            return running, os.getcwd()
    except Exception:
        pass
    return None


def _cmd_backup(a):
    """Dump every stored response to a JSONL file (one {survey_id, response} per line) — a portable,
    store-agnostic cold backup that `survey-agent restore` reloads into any store. Complements
    `defs-export` (which backs up the survey *definitions*)."""
    import survey_agent.store as store_pkg
    store = store_pkg.get_store()
    by = getattr(store, "count_responses_by_survey", None)
    if a.survey:
        ids = [a.survey]
    elif by:
        ids = list(by().keys())
    else:  # store without a survey index (e.g. memory) — derive ids from the rows themselves
        ids = sorted({r.get("survey_id") for r in store.list_responses() if r.get("survey_id")})
    out = a.out or _default_out("responses-backup.jsonl")
    n = 0
    f = sys.stdout if out == "-" else open(out, "w")
    try:
        for sid in ids:
            for r in store.list_responses(survey_id=sid):
                f.write(json.dumps({"survey_id": sid, "response": r}) + "\n")
                n += 1
    finally:
        if out != "-":
            f.close()
    if out != "-":
        print(json.dumps({"backed_up": n, "surveys": len(ids), "file": out}) if a.json
              else f"✅ backed up {n} response(s) across {len(ids)} survey(s) → {out}")
    return 0


def _cmd_restore(a):
    """Reload responses from a `survey-agent backup` JSONL into the configured store. Idempotent:
    a response whose id is already in the store is skipped, so re-running the same restore (or
    restoring over a partially-populated store) never duplicates rows."""
    import survey_agent.store as store_pkg
    store = store_pkg.get_store()
    n = skipped = 0
    have = {}                                   # survey_id -> {response ids already stored}

    def _existing(sid):
        if sid not in have:
            try:
                have[sid] = {r.get("id") for r in store.list_responses(survey_id=sid) if r.get("id")}
            except Exception:
                have[sid] = set()
        return have[sid]
    f = sys.stdin if a.file == "-" else open(a.file)
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            resp = rec.get("response", rec)
            sid = rec.get("survey_id") or resp.get("survey_id")
            if sid is None:
                continue
            rid = resp.get("id")
            seen = _existing(sid)
            if rid and rid in seen:
                skipped += 1
                continue
            store.record_response(sid, resp)
            if rid:
                seen.add(rid)
            n += 1
    finally:
        if a.file != "-":
            f.close()
    print(json.dumps({"restored": n, "skipped_existing": skipped}) if a.json
          else f"✅ restored {n} response(s) into the store"
               + (f" ({skipped} already present — skipped)." if skipped else "."))
    return 0


def _cmd_outbox(a):
    """Inspect / drain the durable webhook outbox (enable with SURVEY_OUTBOX=1). `status` shows
    pending + dead-letter counts; `list [--dead]` shows the queued items; `retry` attempts every due
    delivery now (use from cron, or let the runtime's background sweep handle it)."""
    from survey_agent.integrations import outbox as outbox_mod
    from survey_agent.integrations.webhook import deliver
    ob = outbox_mod.from_env()
    if ob is None:
        msg = "the outbox is off — set SURVEY_OUTBOX=1 (and SURVEY_OUTBOX_DIR / SURVEY_DATA_DIR)."
        print(json.dumps({"error": msg}) if a.json else f"❌ {msg}")
        return 2
    if a.action == "status":
        st = ob.stats()
        print(json.dumps(st) if a.json else f"outbox: {st['pending']} pending, {st['dead']} dead-letter")
    elif a.action == "list":
        items = ob.list(dead=a.dead)
        if a.json:
            print(json.dumps(items, indent=2))
        else:
            print(f"outbox {'dead-letter' if a.dead else 'pending'}: {len(items)} item(s)")
            for i in items:
                print(f"  - {i['id']}  attempts={i.get('attempts')}  {i.get('url') or ''}  {i.get('last_error') or ''}")
    elif a.action == "retry":
        r = ob.retry(deliver)
        print(json.dumps(r) if a.json else
              f"outbox retry: attempted {r['attempted']}, delivered {r['delivered']}, "
              f"dead {r['dead']}, {r['pending']} still pending")
    return 0


def _dsar_id_fields(extra=()):
    """Every url-var name that can carry a respondent's id in this deployment: the generic `uid`, the
    runtime-wide default (SURVEY_RESPONDENT_VAR), any `--id-field`, and every survey's
    `settings.identity.respondent` across every version in the definition store. Returns
    (fields, warnings) — a definition store we can't read is a warning, not a silent miss."""
    from survey_agent.engine import runtime as _rt
    fields, warnings = ["uid"], []

    def add(f):
        if f and str(f) not in fields:
            fields.append(str(f))
    add(_rt.DEFAULT_RESPONDENT_VAR)
    for f in extra or ():
        add(f)
    try:
        from survey_agent import definitions
        ds = definitions.get_definition_store()
        if hasattr(ds, "snapshot_on_read"):          # file backend: inspect only, never write snapshots
            ds.snapshot_on_read = False
        for e in ds.list_surveys():
            sid = e.get("id")
            if not sid or e.get("missing_file"):
                continue
            specs = [ds.get_definition(sid)]
            try:
                specs += [ds.get_definition(sid, v) for v, _, _ in (ds.iter_versions(sid) or [])]
            except Exception:
                pass
            for spec in specs:
                ident = (((spec or {}).get("survey", spec or {}) or {}).get("settings") or {}).get("identity") or {}
                add(ident.get("respondent"))
    except Exception as e:  # noqa: BLE001 — report, don't abort: the defaults still apply
        warnings.append(f"could not read the definition store to find custom identity vars ({e}); "
                        f"matched only on {', '.join(fields)} — add more with --id-field")
    return fields, warnings


def _dsar_delete_uploads(refs):
    """Delete upload blobs by ref via the upload store's delete(). Returns (deleted, errors)."""
    from survey_agent.store import uploads as uploads_mod
    deleted, errors = 0, []
    for ref in refs:
        try:
            kind = ref.get("backend")
            store = uploads_mod.get_upload_store(kind if kind in uploads_mod.BUILTINS else None)
            deleter = getattr(store, "delete", None)
            if deleter is None:
                errors.append(f"{kind}:{ref.get('key')}: the upload store has no delete() — remove it by hand")
                continue
            deleter(ref)
            deleted += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ref.get('backend')}:{ref.get('key')}: {e}")
    return deleted, errors


def _dsar_clear_outbox(webhook_keys):
    """Remove queued (pending + dead-letter) webhook deliveries fired by the erased sessions — they carry
    the respondent's piped data. Matches on the intent's idempotency key. Returns the count removed.
    Runs whether or not SURVEY_OUTBOX is enabled now, since items may remain from when it was."""
    if not webhook_keys:
        return 0
    from survey_agent.integrations import outbox as outbox_mod
    base = os.path.abspath(os.environ.get("SURVEY_OUTBOX_DIR")
                           or os.path.join(os.environ.get("SURVEY_DATA_DIR", "./survey_data"), "outbox"))
    if not os.path.isdir(base):          # nothing was ever queued — don't create the directory
        return 0
    return outbox_mod.Outbox(base).remove_by_idempotency_key(set(webhook_keys))


_DSAR_OUTSIDE = ("Copies already delivered outside this app are NOT erased by this command: webhook / "
                 "panel / CRM passbacks already sent, response backups (`survey-agent backup`), exports "
                 "(CSV/Parquet/reports), log files, and any warehouse or downstream system. Erase the "
                 "subject there too.")


# ---- durable erasure jobs (DSAR) -------------------------------------------------------------
# Erasing a subject spans three seams: the session/response store, the upload (blob) store and the
# webhook outbox. The store returns the blob refs / outbox keys only from the records it deletes, so a
# failed or interrupted blob/outbox removal would otherwise lose the only pointer to that data. Before
# any source record is deleted we therefore persist an *erasure manifest* listing what is still to be
# removed — upload refs and outbox idempotency keys, a hash of the subject id, timestamps; never
# answers — and tick items off as they succeed. The manifest is deleted only when everything is gone.
# Every `dsar` invocation (and `dsar --resume`) first retries any manifest left behind.

_ERASURE_VERSION = 1


def _erasure_dir():
    return os.path.abspath(os.environ.get("SURVEY_ERASURE_DIR")
                           or os.path.join(os.environ.get("SURVEY_DATA_DIR", "./survey_data"), "erasure"))


def _subject_hash(uid):
    import hashlib
    return hashlib.sha256(b"survey-agent/dsar-subject\n" + str(uid).encode("utf-8")).hexdigest()


def _utcnow():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _erasure_save(m):
    """Atomically (re)write a manifest, 0600 in a 0700 directory."""
    d = _erasure_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    m["updated_at"] = _utcnow()
    path = os.path.join(d, f"{m['job_id']}.json")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(m, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def _erasure_remove(m):
    try:
        os.remove(os.path.join(_erasure_dir(), f"{m['job_id']}.json"))
    except FileNotFoundError:
        pass


def _erasure_pending():
    """Every manifest left on disk (oldest first). Never creates the directory. An unreadable manifest
    is reported as an error entry, not silently skipped."""
    d = _erasure_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name)) as f:
                m = json.load(f)
            if not isinstance(m, dict) or not m.get("job_id"):
                raise ValueError("not an erasure manifest")
            out.append(m)
        except Exception as e:  # noqa: BLE001
            out.append({"job_id": name[:-5], "corrupt": f"{e}"})
    return sorted(out, key=lambda m: (m.get("created_at") or "", m["job_id"]))


def _ref_id(ref):
    return (ref.get("backend"), ref.get("bucket"), ref.get("key"))


def _erasure_add(m, refs=(), keys=()):
    have = {_ref_id(r) for r in m["pending"]["upload_refs"]}
    for r in refs or ():
        if isinstance(r, dict) and _ref_id(r) not in have:
            have.add(_ref_id(r))
            m["pending"]["upload_refs"].append(r)
    for k in keys or ():
        if k and k not in m["pending"]["webhook_keys"]:
            m["pending"]["webhook_keys"].append(k)


def _erasure_new(uid, survey_id):
    import secrets
    now = _utcnow()
    return {"version": _ERASURE_VERSION,
            "job_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(6),
            "subject_sha256": _subject_hash(uid), "survey_id": survey_id,
            "state": "erasing_records", "created_at": now, "updated_at": now,
            "pending": {"upload_refs": [], "webhook_keys": []},
            "done": {"uploads_deleted": 0, "outbox_cleared": 0}, "errors": []}


def _erasure_has_pending(m):
    return bool(m["pending"]["upload_refs"] or m["pending"]["webhook_keys"])


def _erasure_run(m):
    """Remove every pending blob and outbox item in `m`, persisting progress after each success. When
    nothing is left (and the records are known erased) the manifest is deleted. Returns a report dict;
    `complete` is False when anything is still pending."""
    errors = []
    for ref in list(m["pending"]["upload_refs"]):
        _, errs = _dsar_delete_uploads([ref])
        if errs:
            errors.extend(errs)
            continue
        m["pending"]["upload_refs"].remove(ref)
        m["done"]["uploads_deleted"] += 1
        _erasure_save(m)
    if m["pending"]["webhook_keys"]:
        try:
            m["done"]["outbox_cleared"] += _dsar_clear_outbox(m["pending"]["webhook_keys"])
            m["pending"]["webhook_keys"] = []
            _erasure_save(m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"outbox: {e}")
    warn = None
    if m.get("state") != "records_erased":
        warn = ("this job was interrupted before the store confirmed the records were erased — re-run "
                "`survey-agent dsar <uid> --delete` for the subject to make sure they are gone")
    m["errors"] = errors
    complete = not _erasure_has_pending(m)
    if complete:
        _erasure_remove(m)
    else:
        _erasure_save(m)
    return {"job_id": m["job_id"], "created_at": m.get("created_at"), "state": m.get("state"),
            "complete": complete, "uploads_deleted": m["done"]["uploads_deleted"],
            "outbox_cleared": m["done"]["outbox_cleared"],
            "uploads_pending": len(m["pending"]["upload_refs"]),
            "outbox_keys_pending": len(m["pending"]["webhook_keys"]),
            "errors": errors, "warning": warn}


def _erasure_resume_all():
    """Retry every pending manifest. Returns a list of per-job reports (corrupt manifests included,
    reported as not complete)."""
    out = []
    for m in _erasure_pending():
        if m.get("corrupt"):
            out.append({"job_id": m["job_id"], "complete": False,
                        "errors": [f"unreadable erasure manifest: {m['corrupt']} — inspect "
                                   f"{os.path.join(_erasure_dir(), m['job_id'] + '.json')}"]})
            continue
        m.setdefault("pending", {}).setdefault("upload_refs", [])
        m["pending"].setdefault("webhook_keys", [])
        m.setdefault("done", {}).setdefault("uploads_deleted", 0)
        m["done"].setdefault("outbox_cleared", 0)
        out.append(_erasure_run(m))
    return out


def _print_resumed(resumed):
    for r in resumed:
        if r.get("complete"):
            print(f"✅ finished pending erasure job {r['job_id']} ({r.get('uploads_deleted', 0)} uploaded "
                  f"file(s), {r.get('outbox_cleared', 0)} queued webhook delivery(ies) removed)")
        else:
            print(f"❌ erasure job {r['job_id']} still incomplete: {r.get('uploads_pending', '?')} "
                  f"upload(s), {r.get('outbox_keys_pending', '?')} outbox key(s) pending — "
                  f"retry with `survey-agent dsar --resume`", file=sys.stderr)
            for e in r.get("errors") or []:
                print(f"   ❌ {e}", file=sys.stderr)
        if r.get("warning"):
            print(f"   ⚠️  {r['warning']}", file=sys.stderr)


def _cmd_dsar(a):
    """Find — and with --delete, erase — everything this app holds for one data subject: responses AND
    sessions (partial, retakes, every survey) in the configured store, their uploaded files, and any
    queued webhook deliveries. The subject is matched on every identity url-var in use (uid, the
    SURVEY_RESPONDENT_VAR default, each survey's settings.identity.respondent, plus --id-field).

    Copies already delivered elsewhere — passbacks, backups, exports, downstream warehouses — are
    outside the app and still yours to erase."""
    import survey_agent.store as store_pkg
    # 1) finish any erasure a previous run left half-done (failed blob delete, crash, …)
    resumed = _erasure_resume_all()
    resumed_failed = any(not r.get("complete") for r in resumed)
    if getattr(a, "resume", False) or not a.uid:
        if not getattr(a, "resume", False):
            msg = "give a respondent id, or --resume to finish pending erasure jobs"
            print(json.dumps({"error": msg}) if a.json else f"❌ {msg}")
            return 2
        if a.json:
            print(json.dumps({"action": "resume", "resumed": resumed}, indent=2))
        elif not resumed:
            print("no pending erasure jobs.")
        else:
            _print_resumed(resumed)
        return 1 if resumed_failed else 0
    if resumed and not a.json:
        _print_resumed(resumed)
    store = store_pkg.get_store()
    uid, sid = a.uid, (a.survey or None)
    fields, warnings = _dsar_id_fields(getattr(a, "id_field", None) or ())
    for w in warnings:
        print(f"⚠️  {w}", file=sys.stderr)
    store_name = os.environ.get("SURVEY_STORE") or "memory"
    try:
        finder = getattr(store, "find_respondent", None)
        if finder is not None:
            found = finder(uid, id_fields=fields, survey_id=sid)
            rows, sessions = found.get("responses", []), found.get("sessions", [])
        else:  # a bring-your-own store without the respondent contract: responses only
            rows, sessions = store.list_responses(survey_id=sid, uid=uid), []
            warnings.append(f"the configured store ({store_name}) has no find_respondent(); sessions "
                            "were not searched")
    except NotImplementedError as e:
        msg = (f"the configured store ({store_name}) can't look a respondent up this way: {e}. Run DSAR "
               "against the runtime's real store (file/sqlite/mongo).")
        print(json.dumps({"error": msg}) if a.json else f"❌ {msg}")
        return 2

    if a.delete:
        eraser = getattr(store, "erase_respondent", None)
        deleter = getattr(store, "delete_responses", None)
        if eraser is None and deleter is None:
            msg = (f"the configured store ({store_name}) has no erase_respondent() — implement it on "
                   "your store to support erasure.")
            print(json.dumps({"error": msg}) if a.json else f"❌ {msg}")
            return 2
        if not a.yes:
            if a.json:
                print(json.dumps({"error": "refusing to delete without --yes (non-interactive)"}))
                return 2
            confirm = input(f"Erase {len(rows)} response(s) and {len(sessions)} session(s) for "
                            f"{uid!r} (matched on {', '.join(fields)})"
                            + (f" in {sid}" if sid else " across all surveys") + "? [y/N] ")
            if confirm.strip().lower() not in ("y", "yes"):
                print("aborted.")
                return 1
        # 2) durable manifest BEFORE deleting any source record: what we learned from the lookup
        known = store_pkg.erasure_result(rows, sessions)
        manifest = _erasure_new(uid, sid)
        _erasure_add(manifest, known["upload_refs"], known["webhook_keys"])
        if _erasure_has_pending(manifest):
            _erasure_save(manifest)
        try:
            if eraser is not None:
                res = eraser(uid, id_fields=fields, survey_id=sid)
            else:
                n = deleter(uid=uid, survey_id=sid)
                res = {"responses": n, "sessions": 0, "upload_refs": [], "webhook_keys": []}
                warnings.append(f"the configured store ({store_name}) has only delete_responses(): "
                                "sessions and uploaded files were NOT erased")
        except NotImplementedError as e:
            _erasure_remove(manifest)            # nothing was erased; the lookup is repeatable
            msg = f"the configured store ({store_name}) can't erase: {e}"
            print(json.dumps({"error": msg}) if a.json else f"❌ {msg}")
            return 2
        # 3) records are gone: fold in anything the store found that the lookup didn't (a racing
        #    write), mark the job, then remove blobs + outbox items, ticking each off as it succeeds
        _erasure_add(manifest, res.get("upload_refs") or [], res.get("webhook_keys") or [])
        manifest["state"] = "records_erased"
        if _erasure_has_pending(manifest):
            _erasure_save(manifest)
        job = _erasure_run(manifest)
        up_deleted, outbox_cleared = job["uploads_deleted"], job["outbox_cleared"]
        up_errors = job["errors"]
        out = {"action": "delete", "uid": uid, "survey_id": sid, "id_fields": fields,
               "deleted": res.get("responses", 0), "sessions_deleted": res.get("sessions", 0),
               "uploads_deleted": up_deleted, "upload_errors": up_errors,
               "outbox_cleared": outbox_cleared, "warnings": warnings, "not_erased": _DSAR_OUTSIDE,
               "erasure_job": None if job["complete"] else job["job_id"], "resumed": resumed}
        if a.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"✅ erased {out['deleted']} response(s), {out['sessions_deleted']} session(s), "
                  f"{up_deleted} uploaded file(s), {outbox_cleared} queued webhook delivery(ies) for "
                  f"{uid!r} (matched on {', '.join(fields)}).")
            for err in up_errors:
                print(f"   ❌ not erased: {err}")
            if not job["complete"]:
                print(f"   ❌ erasure job {job['job_id']} kept with {job['uploads_pending']} upload(s) and "
                      f"{job['outbox_keys_pending']} outbox key(s) pending — fix the cause, then "
                      f"`survey-agent dsar --resume`")
            for w in warnings:
                print(f"   ⚠️  {w}")
            print(f"   {_DSAR_OUTSIDE}")
        return 1 if (up_errors or not job["complete"] or resumed_failed) else 0

    summary = [{"id": r.get("id"), "survey_id": r.get("survey_id"), "status": r.get("status"),
                "date_submitted": r.get("date_submitted")} for r in rows]
    sess_summary = [{"session_id": s.get("session_id"), "survey_id": s.get("survey_id"),
                     "status": s.get("status"), "date_started": s.get("date_started")} for s in sessions]
    if a.json:
        out = {"action": "list", "uid": uid, "survey_id": sid, "id_fields": fields,
               "count": len(rows), "responses": summary,
               "session_count": len(sessions), "sessions": sess_summary, "warnings": warnings,
               "resumed": resumed}
        if getattr(a, "full", False):
            out["records"] = {"responses": rows, "sessions": sessions}
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"DSAR: {len(rows)} response(s), {len(sessions)} session(s) for {uid!r} "
              f"(matched on {', '.join(fields)})" + (f" in {sid}" if sid else " (all surveys)"))
        for r in summary:
            print(f"  - response {r['survey_id'] or '?'}  {r['id'] or '?'}  {r['status'] or ''}  "
                  f"{r['date_submitted'] or ''}")
        for s_ in sess_summary:
            print(f"  - session  {s_['survey_id'] or '?'}  {s_['status'] or ''}  {s_['date_started'] or ''}")
        if rows or sessions:
            print(f"\nFull records (access copy): survey-agent dsar {uid} --json --full")
            print(f"To erase: survey-agent dsar {uid} --delete")
    return 1 if resumed_failed else 0


def _cmd_docs(a):
    """List the docs, or print one. Resolves to a local file in a checkout, else the canonical URL —
    so the pointers scattered through the CLI's output are never a dead end for a pip install."""
    from survey_agent import docs as _docs
    if not a.name:
        print("survey-agent docs — the documentation map\n")
        print("  (↗ = not present locally; open the URL)\n")
        for line in _docs.index_lines():
            print(line)
        print(f"\n  print one:  survey-agent docs <name>        e.g. survey-agent docs logic")
        print(f"  everything: {_docs.REPO}")
        return 0
    name = a.name
    if name not in _docs.DOCS and not name.endswith(".md"):
        close = [k for k in _docs.DOCS if name in k]
        print(f"unknown doc {name!r}." + (f" Did you mean: {', '.join(close)}?" if close else ""))
        print("Run `survey-agent docs` for the list.")
        return 1
    text = _docs.read(name)
    if text is None:
        print(f"Not bundled with the installed package — read it here:\n\n  {_docs.url(name)}")
        return 0
    if a.open:
        import webbrowser
        kind, loc = _docs.resolve(name)
        webbrowser.open(("file://" + loc) if kind == "file" else loc)
        return 0
    print(text)
    return 0


def _cmd_selftest(a):
    """Prove the install works end-to-end: validate + coverage-simulate a survey (the bundled example
    by default) and confirm the engine runs. Zero config, no key, no network — ideal as the first step
    of a walkthrough or a CI smoke. Exits non-zero if anything fails."""
    from survey_agent.engine import validate as v
    from survey_agent import simulate
    survey = a.survey or EXAMPLE
    steps = []
    ok = True
    try:
        spec = json.load(open(survey))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"cannot read {survey}: {e}"}) if a.json
              else f"❌ selftest: cannot read {survey}: {e}")
        return 1

    errs = [e for e in v.validate(spec) if not e.startswith("[warn]")]
    steps.append({"step": "validate", "ok": not errs, "problems": errs})
    ok = ok and not errs

    cov = None
    if not errs:
        cov = simulate.coverage(spec, n=a.n)
        completed = (cov.get("statuses") or {}).get("completed", 0)
        incidence = (completed / a.n) if a.n else 0
        steps.append({"step": "simulate", "ok": not cov["problems"], "problems": cov["problems"],
                      "n": a.n, "completed": completed, "incidence": round(incidence, 3)})
        ok = ok and not cov["problems"]

    if a.json:
        print(json.dumps({"ok": ok, "survey": survey, "steps": steps}, indent=2))
    else:
        print(f"survey-agent selftest — {survey}\n")
        for s in steps:
            print(f"  {'✅' if s['ok'] else '❌'} {s['step']}"
                  + (f" (n={s['n']}, incidence ~{round(100*(s.get('incidence') or 0))}%)"
                     if s['step'] == 'simulate' else ""))
            for p in s["problems"][:5]:
                print(f"       - {p}")
        print("\n" + ("✅ install works — engine validates and simulates cleanly."
                      if ok else "❌ selftest failed — see problems above."))
    return 0 if ok else 1


def _cmd_doctor(a):
    """Diagnose the install + runtime config, and flag the multi-clone editable-install trap (the CLI
    executing a *different* checkout than the folder you're in). Exits non-zero if it finds a mismatch."""
    import importlib.util
    import survey_agent
    from survey_agent import config as config_mod
    running = os.path.realpath(os.path.dirname(survey_agent.__file__))
    ver = survey_agent.__version__
    mm = _editable_mismatch()
    extras = {mod: bool(importlib.util.find_spec(mod)) for mod in ("pymongo", "anthropic", "openai", "mcp")}
    sources = {k: config_mod.source_of(k) for k in ("SURVEY_STORE", "SURVEY_INTEGRATION", "SURVEY_THEME", "SURVEY_DIR")}
    config = {
        "SURVEY_STORE": sources["SURVEY_STORE"][1],
        "SURVEY_DIR": sources["SURVEY_DIR"][1],
        "SURVEY_PUBLIC_BASE_URL": os.environ.get("SURVEY_PUBLIC_BASE_URL") or None,
        "SURVEY_INTEGRATION": sources["SURVEY_INTEGRATION"][1],
        "SURVEY_THEME": sources["SURVEY_THEME"][1],
        "deploy.command": _deploy_command() or None,
        "config_file": config_mod.config_path(),
        "sources": {k: v[0] for k, v in sources.items()},
    }
    # The resolved public base (incl. PaaS autodetect) and where it came from — so a wrong
    # survey_url is traceable to the thing that set it, not guessed at.
    from survey_agent import hosting
    _base = hosting.public_base_url()
    config["public_base_url"] = _base
    config["public_base_url_source"] = hosting.base_url_source()
    config["public_base_reachable"] = not hosting.is_local(_base)
    # Deployment readiness — secret/store/durability checks (no live store ping here: doctor must be
    # fast and must not hang on a DB; the runtime does the live ping at startup).
    from survey_agent import preflight
    checks = preflight.runtime_preflight(ping=False)
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))

    if getattr(a, "json", False):
        print(json.dumps({
            "package": running, "version": ver,
            "editable_mismatch": ({"running": mm[0], "here": mm[1]} if mm else None),
            "extras": extras, "config": config, "llm_key": has_key,
            "deployment_checks": [{"level": lvl, "message": msg} for lvl, msg in checks],
        }, indent=2))
        return 1 if (mm or any(lvl == "fatal" for lvl, _ in checks)) else 0

    print("survey-agent doctor\n")
    print(f"  package : {running}")
    print(f"  version : {ver}")
    if mm:
        running_dir, here = mm
        print("\n  ⚠️  MISMATCH — the CLI is running code from a DIFFERENT checkout than this folder:")
        print(f"        you are here  : {here}")
        print(f"        but executing : {running_dir}")
        print("        → commands use stale code (missing extras/features that live in this repo).")
        print('        Fix: repoint the editable install here →  pip install -e ".[llm,mongo,dev]"')
    elif os.path.isfile(os.path.join(os.getcwd(), "src", "survey_agent", "__init__.py")):
        print("  ✅ editable install points at this repo")
    else:
        print("  (not run from a survey-agent source checkout — nothing to reconcile)")
    print("\n  extras:")
    for label, mod in (("mongo store (pymongo)", "pymongo"), ("Claude ([llm])", "anthropic"),
                       ("GPT ([openai])", "openai"), ("MCP ([mcp])", "mcp")):
        print(f"    {label:22}: {'yes' if extras[mod] else 'no'}")
    src = config["sources"]
    print("\n  config:" + (f"  (config file: {config['config_file']})" if config["config_file"] else
                           "  (no survey-agent.config.yaml here)"))
    print(f"    SURVEY_STORE           : {config['SURVEY_STORE']}  (from {src['SURVEY_STORE']})")
    print(f"    SURVEY_DIR             : {config['SURVEY_DIR'] or 'surveys/'}  (from {src['SURVEY_DIR'] if config['SURVEY_DIR'] else 'default'})")
    print(f"    SURVEY_PUBLIC_BASE_URL : {config['SURVEY_PUBLIC_BASE_URL'] or '(unset)'}")
    _reach = "reachable" if config["public_base_reachable"] else "LOCAL ONLY — respondents cannot open it"
    print(f"    resolved survey base   : {config['public_base_url']}  "
          f"(via {config['public_base_url_source']}; {_reach})")
    print(f"    SURVEY_INTEGRATION     : {config['SURVEY_INTEGRATION']}  (from {src['SURVEY_INTEGRATION']})")
    print(f"    theme                  : {config['SURVEY_THEME'] or 'paper (built-in)'}  "
          f"(from {src['SURVEY_THEME'] if config['SURVEY_THEME'] else 'default'})")
    print(f"    deploy.command         : {config['deploy.command'] or '(none)'}")
    print(f"    LLM key                : {'set' if has_key else '(unset → offline stub)'}")
    print("\n  deployment checks:")
    if not checks:
        print("    ✅ nothing flagged")
    for lvl, msg in checks:
        print(f"    {'✖' if lvl == 'fatal' else ('⚠️ ' if lvl == 'warn' else '·')} {msg}")
    return 1 if (mm or any(lvl == "fatal" for lvl, _ in checks)) else 0


def _cmd_help(a):
    """A guided overview of the commands and the author→launch→report flow — friendlier than the
    argparse `--help` flag dump. Also what bare `survey-agent` (no subcommand) prints."""
    print(r"""survey-agent — author, test, field, and analyze surveys as data.
A survey is a JSON/Markdown file; one small fixed engine runs it (logic is data, never code).

THE FLOW  — author → check → preview → launch → publish → report
  1  author    survey-agent author "screen US adults who stream paid video, quota 300"
                 writes surveys/<slug>.survey.json (+ a readable .md). Needs a model key
                 (ANTHROPIC_API_KEY or OPENAI_API_KEY); validate/simulate/demo run offline.
  2  check     survey-agent validate surveys/<slug>.survey.json     schema + consistency
               survey-agent simulate surveys/<slug>.survey.json -n 500    coverage gate
               survey-agent critique surveys/<slug>.survey.json     methodology review
  3  preview   survey-agent serve --preview --multi --dir surveys   take it at /s/<slug> (drafts too; local only)
               survey-agent convo --survey surveys/<slug>.survey.json    take it as a chat
  4  launch    survey-agent launch surveys/<slug>.survey.json
                 emits the drop-in survey_url + marks it live (LOCAL bookkeeping only).
  5  publish   file backend (default):   git push   (or:  survey-agent launch --deploy)
               runtime store (SURVEY_DEFINITION_SOURCE=mongo/…):   survey-agent publish surveys/<slug>.survey.json
                 publishing is what makes it reachable — launch alone does NOT publish. With a runtime
                 store, `publish` marks it live with NO redeploy; with the file backend you deploy (git).
  6  report    survey-agent report surveys/<slug>.survey.json --open
               survey-agent dashboard --live --open       every survey: URL + live counts

EVERYDAY
  list · status        manage your surveys (lifecycle state, recorded live URL)
  backup · restore · dsar   response backup / idempotent restore / find + erase a subject's data
  export · insights     pull a CSV/Parquet grid · an AI analyst read-out
  init                  scaffold a new project here (config + .env + example survey)
  doctor                check your install + which extras/config are active
  templates · new       list survey templates · scaffold a new survey from one
  copy · templatize     duplicate an existing survey · promote one into a reusable template
  in your agent         /author-survey  /edit-survey  /add-logic  /test-survey  /survey-report

GOOD TO KNOW
  • "launch" marks live locally; PUBLISHING is a separate deploy (git push). survey-agent docs deploy
  • Storage: SURVEY_STORE = memory | file | sqlite | mongo | http | your.pkg:Store (default: file,
    ./survey_data). Set it in .env or survey-agent.config.yaml. The drop-in URL captures all ?params.
  • Definitions are immutable + versioned: in-flight respondents stay on the version they started.

MORE
  survey-agent <command> --help         every flag for a command
  survey-agent docs                     the documentation map (resolves locally, else to a URL)
  survey-agent docs logic|hosting|agents        print one
""")
    return 0


def main(argv=None):
    import survey_agent
    p = argparse.ArgumentParser(prog="survey-agent", description="survey-agent — agentic survey engine")
    p.add_argument("--version", action="version", version=f"survey-agent {survey_agent.__version__}")
    p.set_defaults(fn=_cmd_help)          # bare `survey-agent` (no subcommand) prints the guided help
    sub = p.add_subparsers(dest="cmd", required=False)   # no subcommand → guided help (see _cmd_help)

    s = sub.add_parser("author", help="draft a survey from a plain-language brief (Claude/Codex)")
    s.add_argument("brief", nargs="+", help="what to ask, who to screen, where qualifiers route")
    s.add_argument("--slug", default=None, help="output filename stem (defaults to the survey id)")
    s.add_argument("--out", default="surveys", help="directory to write into (default: surveys/)")
    s.add_argument("--simulate", type=int, default=200, help="N synthetic respondents to coverage-test (0 to skip)")
    s.add_argument("--max-repairs", dest="max_repairs", type=int, default=2,
                   help="how many times to re-prompt the model to fix validation/coverage/incidence gaps")
    s.add_argument("--target-incidence", dest="target_incidence", type=int, default=None,
                   help="target %% who qualify; each valid draft is persona pre-tested against it "
                        "(else inferred from the brief, e.g. 'incidence 40%%')")
    s.add_argument("--target-loi", dest="target_loi", type=int, default=None,
                   help="target length in minutes; each valid draft's estimated LOI is checked "
                        "(else inferred from the brief, e.g. 'under 3 min')")
    s.add_argument("--audience", default=None,
                   help="path to an audience JSON; the incidence gate persona-tests against this population")
    s.set_defaults(fn=_cmd_author)
    s = sub.add_parser("schema", help="print the canonical Survey JSON Schema"); s.set_defaults(fn=_cmd_schema)
    s = sub.add_parser("validate", help="schema + semantic lint of a survey JSON"); s.add_argument("survey")
    s.add_argument("--json", action="store_true", help="machine-readable result {ok, errors, warnings}")
    s.set_defaults(fn=_cmd_validate)
    s = sub.add_parser("simulate", help="coverage gate: N synthetic respondents through every path")
    s.add_argument("survey"); s.add_argument("-n", "--n", type=int, default=500, help="synthetic respondents (default 500)")
    s.add_argument("--quota", type=int, default=0, help="override quota limits to exercise over-quota paths")
    s.add_argument("--out", default=None, help="save the structured coverage result to a JSON file for review")
    s.add_argument("--json", action="store_true", help="machine-readable coverage result to stdout")
    s.set_defaults(fn=_cmd_simulate)
    s = sub.add_parser("pretest", help="persona pre-test: synthetic respondents (incidence preview)")
    s.add_argument("survey")
    s.add_argument("--n", type=int, default=None,
                   help="number of synthetic respondents. Overrides an audience file's `n` when given; "
                        "without it the audience file's `n` is used, else 6. With a real LLM each "
                        "persona makes ~one request per question, so keep this small")
    s.add_argument("--audience", default=None, help="path to an audience JSON (weighted-segment population to test against)")
    s.add_argument("--trait", action="append", default=None, help="k=v trait for a one-off persona (repeatable), e.g. --trait usage=never")
    s.add_argument("--persona", nargs="*", default=None, help="explicit persona description(s) instead of generated ones")
    s.add_argument("--out", default=None, help="save the full run (personas + answers + incidence) to a JSON file")
    s.add_argument("--report", nargs="?", const=True, default=False, help="write an HTML report of this run (optional path)")
    s.add_argument("--open", action="store_true", help="open the HTML report in a browser")
    s.set_defaults(fn=_cmd_pretest)
    s = sub.add_parser("compile", help="survey Markdown → canonical survey JSON")
    s.add_argument("survey"); s.add_argument("--write", action="store_true", help="write <stem>.survey.json next to the .md")
    s.set_defaults(fn=_cmd_compile)
    s = sub.add_parser("render", help="reverse of compile: survey JSON → readable, logic-annotated Markdown")
    s.add_argument("survey")
    s.add_argument("--write", action="store_true", help="write <stem>.md next to the JSON")
    s.set_defaults(fn=_cmd_render)
    s = sub.add_parser("launch", help="emit the drop-in survey_url for your system + mark the survey live")
    s.add_argument("survey")
    s.add_argument("--base", default=None,
                   help="public base URL of your runtime (else $SURVEY_PUBLIC_BASE_URL, then the hosting "
                        "platform's own URL, then http://localhost:8000)")
    s.add_argument("--no-live", action="store_true", help="just print the URL; don't mark it live")
    s.add_argument("--deploy", "--publish", action="store_true", dest="deploy",
                   help="after marking live, run the configured deploy hook (deploy.command in "
                        "survey-agent.config.yaml, or $SURVEY_DEPLOY_COMMAND) to actually publish")
    s.add_argument("--approve", default=None, metavar="TOKEN",
                   help="approval token (required to mark live when SURVEY_LAUNCH_APPROVAL_TOKEN is set)")
    s.add_argument("--ack-responsibilities", action="store_true",
                   help="confirm the operator checklist (store/erasure/consent/URL integrity); required "
                        "to mark live in a token-gated deployment (or set SURVEY_OPERATOR_ACK=1)")
    s.set_defaults(fn=_cmd_launch)
    s = sub.add_parser("sign-url", help="mint a signed per-respondent survey_url (HMAC over uid/rid/group)")
    s.add_argument("survey")
    s.add_argument("params", nargs="+", metavar="key=value",
                   help="the URL variables to sign, e.g. uid=123 rid=456 group=vip")
    s.add_argument("--base", default=None, help="public base URL (else $SURVEY_PUBLIC_BASE_URL)")
    s.set_defaults(fn=_cmd_sign_url)
    s = sub.add_parser("console", help="local operator console — visual board (status, reports, surveys, templates) on 127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--dir", default=None, help="surveys directory for the file backend (default: $SURVEY_DIR or surveys/)")
    s.add_argument("--no-browser", action="store_true", help="don't auto-open a browser")
    s.add_argument("--no-runtime", action="store_true",
                   help="don't auto-start a local respondent runtime for preview (serve it yourself)")
    s.add_argument("--host", default=None,
                   help="bind address (default 127.0.0.1 / $SURVEY_CONSOLE_HOST). Use 0.0.0.0 to host it "
                        "for a team — the console is UNAUTHENTICATED, so keep it behind an auth proxy / VPN")
    s.set_defaults(fn=_cmd_console)
    s = sub.add_parser("api", help="run the API gateway — the authenticated front door to the data backend "
                                   "(console/agent/CLI talk to it via SURVEY_DEFINITION_SOURCE=http / SURVEY_STORE=http)")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8001)
    s.set_defaults(fn=_cmd_api)
    s = sub.add_parser("serve", help="run the survey runtime (one survey, or --multi for all in a dir)")
    s.add_argument("--survey", default=None, help="survey file to serve (default: the bundled example)")
    s.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000; $PORT wins)")
    s.add_argument("--host", default=None, help="bind address (default 127.0.0.1; --public uses 0.0.0.0)")
    s.add_argument("--public", action="store_true",
                   help="bind 0.0.0.0 for public hosting (serves live surveys only)")
    s.add_argument("--preview", action="store_true",
                   help="local preview: serve EVERY lifecycle state (drafts too); refused with --public")
    s.add_argument("--multi", action="store_true",
                   help="serve EVERY survey in --dir at /s/<id> (hosted multi-survey mode)")
    s.add_argument("--dir", default=None,
                   help="surveys directory for --multi (default: $SURVEY_DIR or surveys/)")
    s.set_defaults(fn=_cmd_serve)
    s = sub.add_parser("demo", help="clickable local sandbox (every lifecycle state) + mock webhook receiver")
    s.add_argument("--port", type=int, default=8000, help="runtime port (default 8000)")
    s.add_argument("--survey", default=None, help="path to a survey to demo (default: bundled example)")
    s.add_argument("--port-receiver", dest="port_receiver", type=int, default=9000,
                   help="mock webhook receiver port (default 9000)")
    s.add_argument("--quota", type=int, default=0, help="override quota limits (to see over-quota)")
    s.add_argument("--no-browser", action="store_true", help="don't auto-open a browser")
    s.set_defaults(fn=_cmd_demo)
    s = sub.add_parser("convo", help="take a survey as an AI conversation in the terminal")
    s.add_argument("--survey", default=None, help="survey file (default: the bundled example)")
    s.add_argument("--scripted", nargs="*", default=None, metavar="ANSWER",
                   help="answer non-interactively with these replies in order; exits 1 if they run out "
                        "before a terminal")
    s.set_defaults(fn=_cmd_convo)
    s = sub.add_parser("critique", help="methodology review of a draft (leading/double-barreled/opt-outs/ranges)")
    s.add_argument("survey")
    s.add_argument("--json", action="store_true", help="machine-readable critique {summary, issues}")
    s.set_defaults(fn=_cmd_critique)
    s = sub.add_parser("insights", help="AI analyst read-out of results (headline, findings, themes, next steps)")
    s.add_argument("survey")
    s.add_argument("--simulate", type=int, default=0, help="analyse N synthetic respondents instead of the store")
    s.add_argument("--seed", type=int, default=7); s.set_defaults(fn=_cmd_insights)
    s = sub.add_parser("report", help="self-contained HTML report (funnel, LOI, charts, quotas)"); s.add_argument("survey")
    s.add_argument("--insights", action="store_true", help="add an AI-written insights section (headline, findings, themes)")
    s.add_argument("--simulate", type=int, default=0, help="report on N synthetic respondents instead of the store")
    s.add_argument("--out", default=None, help="output HTML file (default: <output dir>/<id>-report.html)")
    s.add_argument("--seed", type=int, default=7, help="simulation seed")
    s.add_argument("--open", action="store_true", help="open the report in a browser"); s.set_defaults(fn=_cmd_report)
    s = sub.add_parser("export", help="tabular export of responses (CSV or Parquet)"); s.add_argument("survey")
    s.add_argument("--simulate", type=int, default=0, help="export N synthetic respondents instead of the store")
    s.add_argument("--out", default=None, help="output file (default: <output dir>/<id>-responses.<fmt>)")
    s.add_argument("--format", choices=["csv", "parquet"], default=None, help="csv (default) or parquet ([data] extra)")
    s.add_argument("--seed", type=int, default=7, help="simulation seed"); s.set_defaults(fn=_cmd_export)
    s = sub.add_parser("init", help="scaffold a project (config + .env + example) in DIR (default: current folder)")
    s.add_argument("dir", nargs="?", default=".",
                   help="where to scaffold — omit for the current folder, or pass a path like ~/my-surveys")
    s.set_defaults(fn=_cmd_init)
    s = sub.add_parser("list", help="list all surveys with purpose + lifecycle state (validated/tested/live)")
    s.add_argument("--dir", default=None, help="surveys directory (default: $SURVEY_DIR or surveys/)")
    s.add_argument("--json", action="store_true", help="machine-readable catalog")
    s.set_defaults(fn=_cmd_list)
    s = sub.add_parser("publish", help="publish a definition to the store (SURVEY_DEFINITION_SOURCE) + mark live")
    s.add_argument("survey")
    s.add_argument("--dir", default=None, help="surveys directory for the file backend (default: $SURVEY_DIR or surveys/)")
    s.add_argument("--approve", default=None, metavar="TOKEN",
                   help="approval token (required to mark live when SURVEY_LAUNCH_APPROVAL_TOKEN is set)")
    s.set_defaults(fn=_cmd_publish)
    s = sub.add_parser("defs-export", help="dump definitions from the store to local files (cold backup)")
    s.add_argument("--out", default="surveys", help="output directory (default: surveys/)")
    s.add_argument("--all", action="store_true", help="also write every historical version (<id>.v<n>.survey.json)")
    s.set_defaults(fn=_cmd_defs_export)
    s = sub.add_parser("defs-import", help="rebuild the store from local files (DB-loss recovery)")
    s.add_argument("--dir", default=None, help="source directory (default: $SURVEY_DIR or surveys/)")
    s.add_argument("--approve", default=None, metavar="TOKEN",
                   help="approval token (required to mark the imports live when SURVEY_LAUNCH_APPROVAL_TOKEN is set)")
    s.set_defaults(fn=_cmd_defs_import)
    s = sub.add_parser("defs-diff", help="reconcile local files vs the store's live versions")
    s.add_argument("--dir", default=None, help="local directory (default: $SURVEY_DIR or surveys/)")
    s.set_defaults(fn=_cmd_defs_diff)
    s = sub.add_parser("status", help="show or set one survey's lifecycle state / note / live URL")
    s.add_argument("survey_id")
    s.add_argument("--dir", default=None, help="surveys directory (default: $SURVEY_DIR or surveys/)")
    s.add_argument("--set", default=None, help="lifecycle state: draft|validated|tested|approved|live|paused|closed")
    s.add_argument("--note", default=None, help="attach a free-text note")
    s.add_argument("--url", default=None, help="record the live survey_url (use with --set live)")
    s.add_argument("--approve", default=None, metavar="TOKEN",
                   help="approval token (required for --set live when SURVEY_LAUNCH_APPROVAL_TOKEN is set)")
    s.add_argument("--project", default=None,
                   help="move the survey to a project (a mutable group; \"\" clears it / ungroups)")
    s.set_defaults(fn=_cmd_status)
    s = sub.add_parser("dashboard", help="write a self-contained HTML management dashboard (surveys + state + reports)")
    s.add_argument("--dir", default=None, help="surveys directory (default: $SURVEY_DIR or surveys/)")
    s.add_argument("--out", default=None, help="output HTML file (default: dashboard.html)")
    s.add_argument("--live", action="store_true", help="annotate each survey with drop-in URL, params, and live counts (completed/incidence/quota) from the store — no per-survey report files")
    s.add_argument("--base-url", default=None, help="base URL for the drop-in links (default: $SURVEY_PUBLIC_BASE_URL); shows <base>/s/<id> per survey")
    s.add_argument("--reports", action="store_true", help="also generate each survey's report (from the store) and link them")
    s.add_argument("--simulate", type=int, default=0, help="generate linked reports from N synthetic respondents")
    s.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    s.set_defaults(fn=_cmd_dashboard)
    s = sub.add_parser("templates", help="list available survey templates (core / plugin / overlay)")
    s.add_argument("--type", default=None, help="filter by template type (e.g. screener, explainer)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_templates)
    s = sub.add_parser("new", help="scaffold a new survey from a template")
    s.add_argument("template", help="template id (see: survey-agent templates)")
    s.add_argument("--slug", required=True, help="id/slug for the new survey (letters, digits, - or _)")
    s.add_argument("--title", default=None, help="survey title (default: derived from the slug)")
    s.add_argument("--param", action="append", default=[], metavar="K=V", help="template param (repeatable)")
    s.add_argument("--out", default=None, help="output directory (default: the surveys dir)")
    s.add_argument("--simulate", type=int, default=0, help="N synthetic respondents to coverage-test after scaffolding")
    s.add_argument("--force", action="store_true", help="overwrite an existing file")
    s.set_defaults(fn=_cmd_new)
    s = sub.add_parser("copy", help="duplicate an existing survey into a new one (same content, fresh id/version)")
    s.add_argument("source", help="source survey: a slug (in the surveys dir) or a path to a .survey.json")
    s.add_argument("--slug", required=True, help="id/slug for the new survey (letters, digits, - or _)")
    s.add_argument("--title", default=None, help="title for the copy (default: keep the source's title)")
    s.add_argument("--out", default=None, help="output directory (default: the surveys dir)")
    s.add_argument("--force", action="store_true", help="overwrite an existing file")
    s.set_defaults(fn=_cmd_copy)
    s = sub.add_parser("templatize", help="promote an existing survey into a reusable template (inverse of `new`)")
    s.add_argument("source", help="source survey: a slug (in the surveys dir) or a path to a .survey.json")
    s.add_argument("--id", default=None, help="template id (default: derived from the source filename)")
    s.add_argument("--title", default=None, help="template title (default: the source survey's title)")
    s.add_argument("--type", default=None, help="template type for discovery (default: the source's first tag)")
    s.add_argument("--out", default="templates", help="templates dir to write into (default: templates; use overlay/templates for private/brand)")
    s.add_argument("--force", action="store_true", help="overwrite an existing template dir")
    s.set_defaults(fn=_cmd_templatize)
    s = sub.add_parser("backup", help="dump all stored responses to JSONL (portable cold backup)")
    s.add_argument("--survey", default=None, help="limit to one survey id (default: all)")
    s.add_argument("--out", default=None, help="output file (default: reports dir; '-' for stdout)")
    s.add_argument("--json", action="store_true", help="machine-readable summary")
    s.set_defaults(fn=_cmd_backup)
    s = sub.add_parser("restore", help="reload responses from a backup JSONL into the configured store")
    s.add_argument("file", help="the backup .jsonl ('-' reads stdin)")
    s.add_argument("--json", action="store_true", help="machine-readable summary")
    s.set_defaults(fn=_cmd_restore)
    s = sub.add_parser("outbox", help="inspect/drain the durable webhook outbox (SURVEY_OUTBOX=1)")
    s.add_argument("action", choices=["status", "list", "retry"], help="status | list | retry")
    s.add_argument("--dead", action="store_true", help="list: show the dead-letter queue instead of pending")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(fn=_cmd_outbox)
    s = sub.add_parser("dsar", help="find/erase a subject's data — responses, sessions, uploads (DSAR / right-to-be-forgotten)")
    s.add_argument("uid", nargs="?", default=None,
                   help="the respondent id, matched on every identity url var in use (uid, "
                        "SURVEY_RESPONDENT_VAR, each survey's settings.identity.respondent)")
    s.add_argument("--resume", action="store_true",
                   help="only finish pending erasure jobs (blobs / queued webhooks left by a failed or "
                        "interrupted --delete); every dsar run also does this first")
    s.add_argument("--survey", default=None, help="limit to one survey id (default: all surveys in the store)")
    s.add_argument("--id-field", action="append", default=[],
                   help="also match this url var name (repeatable) — e.g. an identity var from a retired survey")
    s.add_argument("--full", action="store_true", help="with --json: include the full records (the access copy)")
    s.add_argument("--delete", action="store_true",
                   help="erase the matching responses, sessions, uploaded files and queued webhooks (default: list only)")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt (required with --delete --json)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(fn=_cmd_dsar)
    s = sub.add_parser("docs", help="list the docs, or print one (resolves locally or to a URL)")
    s.add_argument("name", nargs="?", default=None,
                   help="doc short name (e.g. logic, hosting, agents) — omit to list them all")
    s.add_argument("--open", action="store_true", help="open in a browser instead of printing")
    s.set_defaults(fn=_cmd_docs)
    s = sub.add_parser("selftest", help="prove the install works: validate + simulate a survey (bundled example by default)")
    s.add_argument("survey", nargs="?", default=None, help="survey to test (default: the bundled example)")
    s.add_argument("-n", "--n", type=int, default=100, help="synthetic respondents for the coverage step")
    s.add_argument("--json", action="store_true", help="machine-readable result {ok, steps}")
    s.set_defaults(fn=_cmd_selftest)
    s = sub.add_parser("doctor", help="diagnose the install + config; flags the multi-clone editable-install trap")
    s.add_argument("--json", action="store_true", help="machine-readable diagnosis (extras, config, deployment checks)")
    s.set_defaults(fn=_cmd_doctor)
    s = sub.add_parser("help", help="guided overview of the commands and the author→launch→report flow")
    s.set_defaults(fn=_cmd_help)

    args = p.parse_args(argv)
    _load_dotenv()        # pick up ANTHROPIC_API_KEY etc. from a local .env (never overrides real env)
    # One set of deployment defaults for every command: env (incl. .env) > survey-agent.config.yaml >
    # built-in (SURVEY_STORE=file, SURVEY_INTEGRATION=none) — so serve/demo, report/export/dsar,
    # console and doctor all agree on where responses live.
    from survey_agent import config as _config
    _config.apply_defaults()
    # Deployment default identity: a deployer can make surveys expect e.g. custom respondent/response id params on the way in
    # without setting settings.identity on every survey. The pure engine keeps the generic uid/rid
    # default; we override its module attributes here (post-.env) so all commands + serve pick it up.
    from survey_agent.engine import runtime as _runtime
    _runtime.DEFAULT_RESPONDENT_VAR = os.environ.get("SURVEY_RESPONDENT_VAR") or _runtime.DEFAULT_RESPONDENT_VAR
    _runtime.DEFAULT_RESPONSE_VAR = os.environ.get("SURVEY_RESPONSE_VAR") or _runtime.DEFAULT_RESPONSE_VAR
    # Catch the multi-clone editable-install trap automatically (warn once, to stderr so machine output
    # on stdout stays clean). `doctor` reports it in full; SURVEY_AGENT_NO_DOCTOR=1 silences it.
    if args.cmd not in (None, "help", "doctor") and not os.environ.get("SURVEY_AGENT_NO_DOCTOR"):
        mm = _editable_mismatch()
        if mm:
            running_dir, here = mm
            print(f"⚠️  survey-agent is running code from {running_dir}, not this repo ({here}) — commands "
                  f"may use stale code. Run `pip install -e .` here, or `survey-agent doctor` for details "
                  f"(SURVEY_AGENT_NO_DOCTOR=1 to silence).", file=sys.stderr)
    try:
        return args.fn(args) or 0
    except FileNotFoundError as e:
        fn = getattr(e, "filename", None) or str(e)
        print(f"❌ file not found: {fn}\n"
              f"   Check the path. If you ran `survey-agent init` in another folder, `cd` back to your "
              f"project — or pass an absolute path. List what's here with `survey-agent list`.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
