"""Self-hosting hardening: startup preflight, DSAR erasure, and --json CLI surfaces."""
import json

from survey_agent import preflight
from survey_agent.cli import main


# ---- preflight ----------------------------------------------------------------

def test_preflight_flags_placeholder_secret_for_fielding_integration(monkeypatch):
    monkeypatch.setenv("SURVEY_INTEGRATION", "panel")
    monkeypatch.setenv("SURVEY_PANEL_SHARED_SECRET", "dev-only-change-me")
    issues = preflight.runtime_preflight(store=None, ping=False)
    assert any(lvl == "fatal" and "PANEL_SHARED_SECRET" in msg for lvl, msg in issues)


def test_preflight_memory_store_warns(monkeypatch):
    monkeypatch.setenv("SURVEY_STORE", "memory")
    issues = preflight.runtime_preflight(store=None, ping=False)
    assert any(lvl == "warn" and "memory" in msg for lvl, msg in issues)


def test_preflight_unreachable_networked_store_is_fatal():
    class Boom:
        def quota_counts(self, *a):
            raise RuntimeError("connection refused")
    issues = preflight.runtime_preflight(store=Boom(), store_kind="mongo", ping=True)
    assert any(lvl == "fatal" and "not reachable" in msg for lvl, msg in issues)


def test_preflight_clean_when_configured(monkeypatch):
    monkeypatch.setenv("SURVEY_STORE", "file")
    monkeypatch.delenv("SURVEY_INTEGRATION", raising=False)
    for v in ("RENDER", "K_SERVICE", "FLY_APP_NAME", "DYNO", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(v, raising=False)
    issues = preflight.runtime_preflight(store=None, ping=False)
    assert not [m for lvl, m in issues if lvl in ("fatal", "warn")]


# ---- DSAR erasure via the CLI -------------------------------------------------

def test_dsar_list_then_delete(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_STORE", "sqlite")
    monkeypatch.setenv("SURVEY_SQLITE_PATH", str(tmp_path / "d.db"))
    from survey_agent import store as store_pkg
    s = store_pkg.get_store()
    s.record_response("s1", {"id": "r1", "status": "Complete", "url_variables": {"uid": "u1"}})
    s.record_response("s1", {"id": "r2", "status": "Complete", "url_variables": {"uid": "u2"}})

    assert main(["dsar", "u1", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1 and listed["responses"][0]["id"] == "r1"

    assert main(["dsar", "u1", "--delete", "--yes", "--json"]) == 0
    deleted = json.loads(capsys.readouterr().out)
    assert deleted["deleted"] == 1

    # u1 gone, u2 untouched
    assert store_pkg.get_store().list_responses(uid="u1") == []
    assert len(store_pkg.get_store().list_responses(uid="u2")) == 1


def test_dsar_json_delete_requires_yes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_STORE", "sqlite")
    monkeypatch.setenv("SURVEY_SQLITE_PATH", str(tmp_path / "d.db"))
    code = main(["dsar", "u1", "--delete", "--json"])  # no --yes, non-interactive
    assert code == 2
    assert "error" in json.loads(capsys.readouterr().out)


# ---- --json surfaces ----------------------------------------------------------

def test_validate_json(capsys):
    code = main(["validate", "surveys/streaming-usage.survey.json", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["errors"] == [] and code == 0


def test_doctor_json_has_deployment_checks(capsys):
    main(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert "deployment_checks" in out and "extras" in out and "config" in out


def test_selftest_passes_on_bundled_example(capsys):
    code = main(["selftest", "--json", "-n", "30"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True
    assert {s["step"] for s in out["steps"]} == {"validate", "simulate"}


# ---- review fixes ----

def test_preflight_env_param_is_authoritative():
    """runtime_preflight(env=...) must honor the passed mapping for secrets too, not just os.environ."""
    env = {"SURVEY_INTEGRATION": "panel", "SURVEY_PANEL_SHARED_SECRET": "dev-only-change-me",
           "SURVEY_STORE": "file"}
    issues = preflight.runtime_preflight(env=env, store=None, ping=False)
    assert any(lvl == "fatal" and "PANEL_SHARED_SECRET" in m for lvl, m in issues)


def test_preflight_http_store_is_fatal_with_clear_message():
    issues = preflight.runtime_preflight(env={"SURVEY_STORE": "http"}, store=None, ping=False)
    assert any(lvl == "fatal" and "read-only control-plane" in m for lvl, m in issues)


def test_rate_limiter_fixed_window():
    from survey_agent import ratelimit
    t = [0.0]
    rl = ratelimit.RateLimiter(*ratelimit.parse_rate("3/min"), clock=lambda: t[0])
    assert [rl.allow("ip")[0] for _ in range(4)] == [True, True, True, False]
    assert rl.allow("other")[0] is True          # per-key
    t[0] = 61
    assert rl.allow("ip")[0] is True             # window reset


def test_rate_parse_edge_cases():
    from survey_agent import ratelimit
    assert ratelimit.parse_rate("2/s") == (2, 1)
    assert ratelimit.parse_rate("60/min") == (60, 60)
    assert ratelimit.parse_rate("") is None and ratelimit.parse_rate("nope") is None
    assert ratelimit.parse_rate("0/s") is None   # non-positive → off


def test_backup_restore_round_trip(tmp_path, monkeypatch, capsys):
    import survey_agent.store as store_pkg
    monkeypatch.setenv("SURVEY_STORE", "sqlite")
    monkeypatch.setenv("SURVEY_SQLITE_PATH", str(tmp_path / "src.db"))
    s = store_pkg.get_store()
    s.record_response("s1", {"id": "a", "status": "Complete", "url_variables": {"uid": "u1"}})
    s.record_response("s2", {"id": "b", "status": "Disqualified", "url_variables": {"uid": "u2"}})
    bk = str(tmp_path / "bk.jsonl")
    assert main(["backup", "--out", bk, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["backed_up"] == 2

    monkeypatch.setenv("SURVEY_SQLITE_PATH", str(tmp_path / "dst.db"))  # fresh target
    assert main(["restore", bk, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["restored"] == 2
    dst = store_pkg.get_store()
    assert len(dst.list_responses("s1")) == 1 and len(dst.list_responses("s2")) == 1


def test_outbox_retry_then_dead_letter(tmp_path):
    import time
    from survey_agent.integrations.outbox import Outbox
    ob = Outbox(dir=str(tmp_path / "ob"), max_attempts=2)
    ob.deliver_or_enqueue({"url": "http://x", "payload": {"idempotency_key": "k1"}}, lambda i: False)
    assert ob.stats() == {"pending": 1, "dead": 0}
    base = time.time() + 10_000                       # past the enqueue backoff
    assert ob.retry(lambda i: False, now=base)["attempted"] == 1
    r2 = ob.retry(lambda i: False, now=base + 10_000)  # exceeds max_attempts → dead-letter
    assert r2["dead"] == 1 and ob.stats() == {"pending": 0, "dead": 1}
    assert ob.list(dead=True)[0]["id"]


def test_outbox_delivers_on_retry_and_dedupes(tmp_path):
    import time
    from survey_agent.integrations.outbox import Outbox
    ob = Outbox(dir=str(tmp_path / "ob"))
    intent = {"url": "http://z", "payload": {"idempotency_key": "dupe"}}
    ob.enqueue(intent)
    ob.enqueue(intent)                                # identical → same file
    assert ob.stats()["pending"] == 1
    r = ob.retry(lambda i: True, now=time.time() + 10_000)
    assert r["delivered"] == 1 and ob.stats()["pending"] == 0


def test_outbox_cli_off_without_env(capsys, monkeypatch):
    monkeypatch.delenv("SURVEY_OUTBOX", raising=False)
    assert main(["outbox", "status", "--json"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_obs_json_logger_emits_extras(capsys):
    from survey_agent import obs
    obs.configure(force=True, env={"SURVEY_LOG_FORMAT": "json", "SURVEY_LOG_LEVEL": "INFO"})
    obs.get_logger("survey_agent.t").warning("delivery failed", extra={"survey_id": "s1", "url": "http://x"})
    rec = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert rec["msg"] == "delivery failed" and rec["survey_id"] == "s1" and rec["level"] == "WARNING"
    obs.configure(force=True, env={})                # restore default text format for other tests


def test_dsar_list_handles_store_without_full_list(capsys, monkeypatch):
    """A store whose list_responses raises NotImplementedError yields a clean error, not a traceback."""
    import survey_agent.store as store_pkg

    class ROStore:
        def list_responses(self, survey_id=None, uid=None):
            raise NotImplementedError("requires a survey_id")
    monkeypatch.setattr(store_pkg, "get_store", lambda *a, **k: ROStore())
    code = main(["dsar", "u1", "--json"])
    assert code == 2 and "error" in json.loads(capsys.readouterr().out)
