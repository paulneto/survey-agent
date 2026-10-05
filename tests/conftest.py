"""Shared pytest fixtures / isolation.

Two cross-test leaks to neutralize, both rooted in a CLI test calling `cli.main` → `load_dotenv`,
which injects this deployment's local `.env` into the process:

1. `load_dotenv` fills *unset* env keys (SURVEY_PREVIEW_TOKEN, SURVEY_SERVE_STATES, SURVEY_INTEGRATION,
   …) from `.env`; they then persist and bleed into later tests that assume those vars are unset.
2. `cli.main` overrides the engine's default identity url-vars by assigning module attributes on
   `engine.runtime` (from SURVEY_RESPONDENT_VAR/RESPONSE_VAR).

We snapshot the clean baseline at collection time (before any test runs `load_dotenv`). Before each
test we strip any env key not in that baseline (kills the `.env` bleed without touching real env or
fighting monkeypatch, which manages its own keys during the test). After each test we restore the
engine defaults. Net: the suite is order- and `.env`-independent — green with or without a local `.env`.
"""
import os

import pytest

from survey_agent.engine import runtime as _runtime

_ORIG_IDENTITY_DEFAULTS = (_runtime.DEFAULT_RESPONDENT_VAR, _runtime.DEFAULT_RESPONSE_VAR)
_BASELINE_ENV_KEYS = set(os.environ)


@pytest.fixture(autouse=True)
def _isolate_env_and_defaults():
    for k in [k for k in os.environ if k not in _BASELINE_ENV_KEYS]:
        del os.environ[k]
    yield
    _runtime.DEFAULT_RESPONDENT_VAR, _runtime.DEFAULT_RESPONSE_VAR = _ORIG_IDENTITY_DEFAULTS
