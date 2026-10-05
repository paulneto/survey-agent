"""Public-base-URL resolution — the drop-in survey_url's base is the product's contract with the
outside world, and a localhost link handed to a panel is a silent, total failure. These tests pin
the resolution order and the reachability check."""
from survey_agent import hosting


def test_default_is_localhost_when_nothing_is_set():
    assert hosting.public_base_url(env={}) == hosting.LOCAL_DEFAULT
    assert hosting.base_url_source(env={}) == "default"


def test_explicit_override_wins_over_everything():
    env = {"SURVEY_PUBLIC_BASE_URL": "https://configured.example.com",
           "RENDER_EXTERNAL_URL": "https://render.example.com"}
    assert hosting.public_base_url("https://cli.example.com", env=env) == "https://cli.example.com"
    assert hosting.base_url_source("https://cli.example.com", env=env) == "explicit"


def test_operator_config_wins_over_host_autodetect():
    """An operator who set the variable meant it — a custom domain must not be overridden by the PaaS."""
    env = {"SURVEY_PUBLIC_BASE_URL": "https://surveys.acme.com",
           "RENDER_EXTERNAL_URL": "https://my-surveys.onrender.com"}
    assert hosting.public_base_url(env=env) == "https://surveys.acme.com"
    assert hosting.base_url_source(env=env) == "SURVEY_PUBLIC_BASE_URL"


def test_host_autodetect_per_platform():
    """This is what makes a one-click deploy produce a working survey_url with no follow-up step."""
    cases = [
        ({"RENDER_EXTERNAL_URL": "https://my-surveys.onrender.com"}, "https://my-surveys.onrender.com", "Render"),
        ({"RAILWAY_PUBLIC_DOMAIN": "my-surveys.up.railway.app"}, "https://my-surveys.up.railway.app", "Railway"),
        ({"FLY_APP_NAME": "my-surveys"}, "https://my-surveys.fly.dev", "Fly.io"),
        ({"VERCEL_PROJECT_PRODUCTION_URL": "my-surveys.vercel.app"}, "https://my-surveys.vercel.app", "Vercel"),
        ({"HEROKU_APP_NAME": "my-surveys"}, "https://my-surveys.herokuapp.com", "Heroku"),
    ]
    for env, expected_url, expected_label in cases:
        assert hosting.public_base_url(env=env) == expected_url, env
        assert hosting.base_url_source(env=env) == expected_label, env
        assert hosting.detect_host(env) == (expected_label, expected_url)


def test_render_is_preferred_when_several_host_vars_are_present():
    env = {"RENDER_EXTERNAL_URL": "https://r.onrender.com", "FLY_APP_NAME": "f"}
    assert hosting.public_base_url(env=env) == "https://r.onrender.com"


def test_values_copied_from_env_example_are_tolerated():
    """A value pasted with an inline comment, quotes or a trailing slash must still resolve."""
    for raw in ('https://x.example.com/  # the public base', '"https://x.example.com"',
                "'https://x.example.com'", "https://x.example.com/"):
        assert hosting.public_base_url(env={"SURVEY_PUBLIC_BASE_URL": raw}) == "https://x.example.com"


def test_no_host_detected_returns_none_pair():
    assert hosting.detect_host({}) == (None, None)


def test_is_local_matches_on_hostname_not_substring():
    for local in ("http://localhost:8000", "http://127.0.0.1:9000", "http://0.0.0.0:8000",
                  "http://[::1]:8000", "http://mymac.local:8000"):
        assert hosting.is_local(local), local
    for public in ("https://my-surveys.onrender.com", "https://surveys.acme.com",
                   "https://localhost.example.com"):
        assert not hosting.is_local(public), public


def test_unreachable_warning_only_fires_for_local_bases():
    warn = hosting.unreachable_warning("http://localhost:8000")
    assert warn and "cannot open it" in warn
    assert hosting.unreachable_warning("https://my-surveys.onrender.com") is None


def test_empty_default_falls_back_to_localhost_not_empty_string():
    """An empty `default` must not yield an empty base — a link with no host is worse than a local
    one. Callers that genuinely want relative links (the dashboard) decide that themselves."""
    assert hosting.public_base_url(env={}, default="") == hosting.LOCAL_DEFAULT
    assert hosting.public_base_url(env={"SURVEY_PUBLIC_BASE_URL": "https://a.example.com"},
                                   default="") == "https://a.example.com"
