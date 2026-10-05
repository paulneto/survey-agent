"""survey-agent API gateway (FastAPI) — the authenticated front door to the data backend.

The gateway is the **sole owner of the database connection** (Mongo/file). The console, the MCP agent,
and the CLI talk to *it* over HTTP (bearer auth) instead of opening their own DB connection — one
authenticated, auditable surface in front of the data, so an operator console can be hosted for a team
without handing every process raw DB credentials.

It exposes the existing seams: the full `definitions` interface (specs + lifecycle) and the read side of
the response `store`. Clients opt in with `SURVEY_DEFINITION_SOURCE=http` / `SURVEY_STORE=http` +
`SURVEY_API_BASE` + `SURVEY_API_TOKEN` — the HTTP backends are pure stdlib, so a client needs no extra
deps. Only the gateway host installs the `[api]` extra (fastapi + uvicorn).

Run it where the database lives:
    SURVEY_DEFINITION_SOURCE=mongo SURVEY_STORE=mongo SURVEY_API_TOKEN=… survey-agent api --host 0.0.0.0
"""
