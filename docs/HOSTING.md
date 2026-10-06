# Hosting survey-agent (low-friction options)

> **What's real today.** The **respondent runtime** (`survey-agent serve`: serving `survey_url` pages
> from the `file`, `sqlite` or `mongo` store, themes, terminal passbacks) and the **CLI/MCP authoring
> and management loop** are the core and are tested. The **API gateway** (`survey-agent api`, the
> `[api]` extra) serves definitions, lifecycle and read-side response access over HTTP; it **runs open
> unless you configure auth** (see [The API gateway](#the-api-gateway--one-authenticated-front-door-to-the-data)).
> **Erasure** is built in: `survey-agent dsar <id> --delete` erases a subject's responses, sessions,
> uploaded files and queued webhook deliveries; copies outside the app (passbacks already sent,
> exports, backups, logs, downstream systems) are yours to clear. Terminal passbacks are **best-effort**
> and carry an idempotency key (a retried completion can repeat one, always with the same key); the optional durable outbox (`SURVEY_OUTBOX=1`) covers mid-survey webhook
> **actions** only. For a local or single-box deployment none of this bites; for a shared team
> deployment, plan for it.

## What does your deployment need? (start simple)

Most of survey-agent is optional. Pick the smallest answer to each question; you can add pieces
later. **Mongo, Render, Fly, Firebolt and Mailchimp below are reference implementations of swappable
seams, not requirements.**

| Question | If… | Use |
|---|---|---|
| **Where do responses live?** | just trying it / CI | the default `file` store (`./survey_data`), or `memory` for throwaway tests |
| | one box, one worker process, a persistent disk | `file` (the default) — flat JSON/JSONL, zero infra, grep-able |
| | one box, **multiple** workers, still no server | **`sqlite`** — one file, concurrency-safe (stdlib) |
| | multiple hosts / scale-to-zero / ephemeral disk | a database — the reference `mongo` store **or** your own (`SURVEY_STORE=your.pkg:Store`; Postgres, DynamoDB, Redis, your warehouse) |
| **Where does it run?** | a public `survey_url` for fielding | any container/serverless/PaaS host (Cloud Run, Render, Fly, Koyeb, your own VM, … — the `Dockerfile` runs unchanged) |
| | only authoring/testing | nothing to host — it's all local CLI/MCP |
| **Pass results back to a panel/CRM?** | no | `SURVEY_INTEGRATION=none` (the default) |
| | yes | `redirect` / `webhook` (built-in), `panel` (a generic signed passback), `mailchimp` (reference example), or your own plugin |
| **Enrich answers from behavioral data?** | no (most cases) | skip it (`SURVEY_ENRICH` unset) |
| | yes | your warehouse via an adapter (the shipped `firebolt` adapter is a reference example) |
| **Natural-language authoring / conversation / analysis?** | yes | install `[llm]` (Anthropic) or `[openai]` and set the key; without one, a deterministic offline stub runs |
| **A team sharing one backend?** | yes | run the API gateway (`[api]`) as the sole database owner, **with auth configured**; clients talk HTTP with no database credentials |

If you're a single operator on one box, **`file` (or `sqlite`) on a host with a persistent disk** is
the whole answer: no database, no gateway.

## The shape: local control plane + a small hosted runtime

There are two planes, and only one of them needs hosting:

- **Control plane, local.** Authoring, validating, simulating, reviewing, and managing surveys run on
  your machine through your agent (any MCP-capable client, or a coding agent with a shell) and the
  `survey-agent` CLI. No public exposure, no infra, and it works offline.
- **Data plane, a small hosted runtime.** Fielding to real people needs a publicly reachable URL, so
  the **respondent runtime** lives on a host, not your laptop. It needs the survey *definitions*
  (baked into the image, synced from git, or read from a definition store), a response store, and your
  integration config. With file-based definitions, deploying a new survey is a `git push`.

A **scale-to-zero** host keeps cost near zero for occasional traffic, but its filesystem is
ephemeral, so pair it with a database. A single always-on box with a persistent disk and the `file`
or `sqlite` store needs no database at all. Your authoring model keys (`ANTHROPIC_API_KEY` /
`OPENAI_API_KEY`) can stay on your machine; the respondent runtime needs one only if you use
conversation mode or `source: llm` analysis.

survey-agent has three deployment shapes; pick the smallest that fits:

1. **CLI only:** `survey-agent author / validate / simulate / demo`. No server, no database. Runs on
   your laptop or a CI runner.
2. **The runtime:** the stdlib `survey-agent serve` runtime serving `survey_url` pages over HTTP. The
   repo's `Dockerfile` (a `python:3.12-slim` image) runs it on `:8000`. Storage is flat files by
   default; step up to `sqlite` or a database as needed. See [Storage](#storage-pick-the-simplest-rung-that-fits).
3. **A remote MCP connector:** only if you want a web chat client to author against a *hosted*
   deployment. Needs a public **HTTPS** endpoint with OAuth in front, which you provide. For local
   desktop/CLI agents you host nothing; the connector runs locally over stdio.

## Host the try-in-browser demo (the agentic showcase)

A public URL where anyone can take a survey as a **form** and try it as an **AI conversation**
(`/chat`), with no install for the respondent:

```bash
SURVEY_CHAT_DEMO=1 survey-agent serve --public
```

The browser chat is a demo surface: it is served only in demo/preview mode or with
`SURVEY_CHAT_DEMO=1`, and it records nothing. Leave `SURVEY_CHAT_DEMO` unset on a runtime that
fields real respondents.

What `--public` does: binds `0.0.0.0` and serves only surveys whose
lifecycle state is `live` (configurable with `SURVEY_SERVE_STATES`; anything pre-live shows a "not
open yet" page). Mark the survey you front as live first (`survey-agent status <id> --set live`).
Storage and passbacks follow your configuration like everywhere else (`SURVEY_STORE` unset = `file`
under `./survey_data`; `SURVEY_INTEGRATION` unset = `none`, so no outbound passback calls), so on an
ephemeral host the demo's responses disappear on restart — fine for a demo, not for fielding.

A [`Procfile`](../Procfile) ships for PaaS hosts that read one:

```
web: survey-agent serve --public
```

On a PaaS (Render, Railway, Koyeb, …): create a web service from your repo, build with
`pip install ".[llm]"`, start with `survey-agent serve --public`. Add `ANTHROPIC_API_KEY` (or
`OPENAI_API_KEY`) as a secret for natural conversational phrasing, and `SURVEY_CHAT_DEMO=1` to serve
the `/chat` demo; with no key it runs on the deterministic offline stub. Front a specific survey with
`SURVEY_FILE=surveys/<slug>.survey.json`. Conversation mode sends respondents' replies to that model
provider — see [DATA_SECURITY.md](DATA_SECURITY.md#where-respondent-data-can-reach-an-llm).

For a real multi-survey deployment (`serve --public --multi`, a durable store, auto-deploy), see
[DEPLOY.md](DEPLOY.md).

## One-click deploy (the fastest path to a fieldable `survey_url`)

The only thing between an agent-authored survey and a real respondent is a **publicly reachable
runtime**. On the hosts below the runtime **derives its public base URL from the host's environment**,
so the drop-in `survey_url` is right immediately. `SURVEY_PUBLIC_BASE_URL` is optional — set it only
to override (e.g. a custom domain) or on a host that isn't auto-detected.

| Host | Command / action | Reads | Notes |
|---|---|---|---|
| **Render** | Blueprint → connect repo → Apply ([`render.yaml`](../render.yaml)) | `RENDER_EXTERNAL_URL` | **uses a paid plan** (`plan: starter`; a persistent disk needs a paid instance). Not zero-downtime — see the file header |
| **Render (Mongo)** | copy [`render.mongo.yaml`](../render.mongo.yaml) over `render.yaml` | `RENDER_EXTERNAL_URL` | diskless, rolling deploys; ships as `plan: starter` (paid) |
| **Fly.io** | `fly launch --copy-config --no-deploy && fly deploy` ([`fly.toml`](../fly.toml)) | `FLY_APP_NAME` | machines auto-stop when idle |
| **Railway** | new project from repo | `RAILWAY_PUBLIC_DOMAIN` | no config file needed |
| **Docker / anywhere else** | [`Dockerfile`](../Dockerfile) | — | set `SURVEY_PUBLIC_BASE_URL` yourself |

The Render button is "one click" to provision, but the blueprint as shipped is not free: it requests
the paid Starter plan. Edit `plan:` in the file if you want something else (a persistent disk still
requires a paid instance).

Confirm the result with:

```bash
survey-agent doctor          # → "resolved survey base : https://…  (via Render; reachable)"
```

If it says **`LOCAL ONLY — respondents cannot open it`**, the `survey_url` you hand to a panel will
reach nobody. `survey-agent launch` prints the same warning when it hands over the URL.

### The loop this enables

```
  agent authors the survey          →  validate + simulate (agent, autonomous)
  human deploys once                →  the host injects its public URL
  human approves the launch         →  agent hands back the survey_url
  operator drops it into their panel / CRM / email
```

Deploying is a one-time setup per instance, not per survey. After that, publishing a new survey is
either a `git push` (file definitions) or `survey-agent publish` with no redeploy (a runtime
definition store) — see [DEPLOY.md](DEPLOY.md).

> **What a deploy does *not* do:** field the survey. It gives you a reachable runtime. You still bring
> the audience (sampling, panel and incentives are out of scope), and you still own the operator
> responsibilities `launch` prints: durable store, erasure path, consent, URL integrity, HTTPS. See
> [OPERATOR_RESPONSIBILITIES.md](OPERATOR_RESPONSIBILITIES.md).

## Host shortlist

Provider plans, free tiers and limits change often. The notes below are qualitative and were
accurate **as of 2026-10**; check each provider's current pricing before you commit.

1. **Google Cloud Run** — deploys the `Dockerfile` directly, scales to zero, gives an HTTPS URL.
   Stateless filesystem, so use a database store. Not auto-detected: set `SURVEY_PUBLIC_BASE_URL`.
2. **Render** — low-friction Docker/Python hosting with automatic HTTPS. Two Blueprints ship in the
   repo as worked examples: [`render.yaml`](../render.yaml) (flat files on a persistent disk — simple,
   low-traffic, **not** zero-downtime) and [`render.mongo.yaml`](../render.mongo.yaml) (diskless +
   MongoDB — rolling, zero-downtime deploys). Both request a paid plan. See
   [DEPLOY.md](DEPLOY.md#appendix-a--one-time-setup-the-service).
3. **Fly.io** — a good fit when you want a **real persistent volume** (the flat-file shape) with
   machines that stop when idle. [`fly.toml`](../fly.toml) ships.
4. **Koyeb**, **Hugging Face Spaces (Docker)**, **Railway** — container hosts that work unchanged;
   free/cheap tiers typically have ephemeral storage, so pair with a database.
5. **Your own VM or Kubernetes** — run the `Dockerfile` (or `pip install` + `survey-agent serve`)
   behind your existing TLS/proxy; a persistent disk makes `file`/`sqlite` viable.

### Comparison (as of 2026-10)

| Host | Sleeps idle? | Docker? | Persistent disk? | Auto-detected base URL | Gotcha |
|---|---|---|---|---|---|
| **Google Cloud Run** | Yes (scale-to-zero) | Yes | No | No | Ephemeral FS → database store |
| **Render** | Paid plans stay up | Yes | Paid add-on | Yes | Disk-backed services restart (not zero-downtime) on deploy |
| **Fly.io** | Configurable (auto-stop) | Yes | Volumes | Yes | Volume pins one machine |
| **Koyeb** | Free tier sleeps | Yes | No on free | No | Ephemeral FS → database store |
| **Hugging Face Spaces** | Free tier sleeps | Yes | Paid add-on | No | Ephemeral storage by default |
| **Railway** | Configurable | Yes | Volumes | Yes | No permanent free tier |

## Storage: pick the simplest rung that fits

Climb only as far as your deployment forces you:

1. **`file`** (the default when `SURVEY_STORE` is unset) — flat JSON/JSONL under `./survey_data`
   (or `SURVEY_DATA_DIR`). Inspectable, grep-able, zero infra. Safe for a **single worker process** on
   a **persistent disk**. Quota reservations are file-locked, but sessions and responses aren't
   locked across processes, so it isn't safe once more than one process writes.
2. **`sqlite`** — one SQLite file with atomic quota reservation that is safe across **threads and
   processes** (WAL mode). Still pure stdlib, no server. The right step up when you run more than one
   worker on a single box. Path via `SURVEY_SQLITE_PATH`.
3. **A database** — once storage is **shared across hosts** or the disk is **ephemeral**:
   - **Bring your own.** Point `SURVEY_STORE` at an import path (`my_pkg.store:PostgresStore`) and
     implement the store contract (see `src/survey_agent/store/__init__.py`). Postgres, DynamoDB,
     Redis, your warehouse — whatever you already run. These adapters are not shipped.
   - **The reference Mongo store** (`SURVEY_STORE=mongo`, the `[mongo]` extra) — a worked example of
     the contract, not a default. Point it at any MongoDB (self-hosted or a managed service) with
     `SURVEY_MONGODB_URI`. PaaS hosts usually have dynamic outbound IPs, so you may need to widen the
     database's network access list accordingly.

> **Ephemeral-disk trap.** On scale-to-zero/ephemeral hosts the filesystem is wiped on restart or
> redeploy, so `file`/`sqlite` there silently lose data — use rung 3. The runtime's startup preflight
> warns when it sees `SURVEY_STORE=file` on a host that looks ephemeral, and on the `memory` store. A
> real persistent volume (e.g. a Fly volume or a VM disk) makes `file`/`sqlite` fine.

> **Definitions vs responses.** By default (`SURVEY_DEFINITION_SOURCE=file`) survey *definitions* are
> files (in git) and only sessions, quota counters and responses go to the response store. With a
> runtime definition store (e.g. `=mongo`, or `http` via the gateway) definitions and lifecycle are
> published to that store instead (no redeploy, version-pinned), with local files as the authored
> artifact and backup. The response store (`SURVEY_STORE`) is a separate seam.

> **The runtime enforces the lifecycle.** `survey-agent serve --public` serves real respondents only
> when a survey's state is in `SURVEY_SERVE_STATES` (default `live`). `paused`/`closed` show their
> pages and start no new sessions; pre-live states show a "not open yet" page. To preview a pre-live
> survey on a hosted runtime, set `SURVEY_PREVIEW_TOKEN` and open `/s/<id>?preview=<token>` (preview
> collects nothing). Locally, `survey-agent serve --preview` serves drafts for previewing, and
> `survey-agent demo` serves every state.

## Operating the runtime (healthcheck, shutdown, secrets)

Things the runtime does so it behaves on a real host:

- **Healthcheck.** `GET /health` returns `{"ok": true}` (200). It returns **503** while the process
  is draining. Set `SURVEY_HEALTH_CHECK_STORE=1` to also have `/health` ping the store (cached 10s)
  and 503 if it's unreachable. The API gateway also serves `GET /health`.
- **Graceful shutdown.** On `SIGTERM`/`SIGINT` the runtime stops accepting new connections, lets
  in-flight requests finish, and closes the socket.
- **Startup checks.** Before binding the port the runtime runs a preflight and **refuses to start** on
  a fatal misconfiguration (e.g. a fielding integration still using the dev-placeholder signing
  secret, or an unreachable database). It **warns** but starts on the in-memory store or a `file`
  store on an ephemeral-looking host. `SURVEY_ALLOW_INSECURE=1` overrides a fatal (not recommended).
  Run the same checks any time with `survey-agent doctor` (`--json` for machine-readable output).
- **Secrets.** Set the secrets you use to strong random values (`openssl rand -hex 32`):
  `SURVEY_PANEL_SHARED_SECRET` (signs panel passbacks), `SURVEY_WEBHOOK_SECRET` (signs outbound
  webhook posts), `SURVEY_URL_SECRET` (signs/verifies respondent URLs), and the gateway's
  `SURVEY_SESSION_JWT_KEY` / `SURVEY_API_TOKEN`. Never ship the `.env.example` placeholders.
- **TLS.** The runtime speaks plain HTTP; your host or proxy terminates HTTPS.
- **Request limits.** Request bodies over `SURVEY_MAX_BODY_BYTES` (default 10 MB) get a 413 before
  anything is read; the chat JSON endpoint is capped at 64 KB. Each connection has a socket timeout of
  `SURVEY_REQUEST_TIMEOUT` seconds (default 30), so a stalled client can't hold a worker. File uploads
  are checked against the question's count, extension and size rules before anything is stored.
- **Response headers and logs.** Survey pages and data responses are sent with
  `Cache-Control: no-store`, and every runtime response with `Referrer-Policy: no-referrer` and
  `X-Content-Type-Options: nosniff`. Log lines carry a short hash in place of session and response
  ids, and URLs (`url` and any `*_url` field) reduced to `scheme://host[:port]#<8-hex fingerprint>`:
  never the path, query or userinfo, since a webhook URL often carries its secret in the path.
- **What goes into the image.** `.dockerignore` excludes everything by default and re-includes only
  what the image needs: `src/`, the packaging files, `surveys/*.survey.json`, `surveys/*.md`,
  `surveys/.registry.json`, `surveys/.versions/` and `triggers/*.json`. Local data (`survey_data/`,
  uploads, exports, `.env` files, databases) never reaches the build. If your runtime reads another
  path, add it there.

## The API gateway — one authenticated front door to the data

> **The gateway runs open — no authentication — unless you configure it.** Set at least a bearer
> token (`SURVEY_API_TOKEN`), or use `sa_` API keys or Google OAuth (the `[auth]` extra; see *Auth
> modes* below). `survey-agent api` prints a warning when it starts without `SURVEY_API_TOKEN`.

For a team deployment, don't give every process (the operator console, an MCP agent, the CLI) a
direct database connection. Run the gateway where the database lives; it becomes the sole database
owner and exposes definitions, lifecycle and read-side response access over HTTP:

```bash
# on the box with the database:
pip install 'survey-agent[api,mongo]'
SURVEY_DEFINITION_SOURCE=mongo SURVEY_STORE=mongo SURVEY_MONGODB_URI=… \
  SURVEY_API_TOKEN="$(openssl rand -hex 24)" survey-agent api --host 0.0.0.0 --port 8001
```

Then point the console / agent / CLI at it; they become HTTP clients with no extra dependency (the
client backends are pure stdlib):

```bash
SURVEY_DEFINITION_SOURCE=http SURVEY_STORE=http \
  SURVEY_API_BASE=https://api.example.internal SURVEY_API_TOKEN=… survey-agent console
```

**Auth modes.** `pip install 'survey-agent[api]'` runs the gateway in bearer-token mode
(`SURVEY_API_TOKEN` and `sa_` API keys). Google sign-in needs
`[api,auth]` and `SURVEY_GOOGLE_CLIENT_ID` / `SURVEY_GOOGLE_CLIENT_SECRET` (optionally
`SURVEY_OAUTH_ALLOWED_DOMAIN`):

- Session cookies are honoured only when Google sign-in is configured **and**
  `SURVEY_SESSION_JWT_KEY` is a strong secret (not the dev default, at least 32 characters, e.g.
  `python -c 'import secrets; print(secrets.token_urlsafe(48))'`). In every other mode cookies are
  ignored. A session token must carry valid `exp`, `iat`, `aud` and `sub` claims.
- With Google sign-in configured, the gateway refuses to start if the session key is weak or the
  `[auth]` extra is missing (it prints the install hint).
- The `/keys` page, where a signed-in person mints and revokes `sa_` keys, is served only when Google
  sign-in is configured.

**Lifecycle writes.** `POST /v1/registry/<id>` accepts only `status`, `note`, `project`, `live`,
`live_version`, `validated` and `tested`; anything else is a 400. A write whose resulting state is
`live`, and that newly makes the survey live or touches `status`, `live` or `live_version`, needs the
launch approval (`X-Survey-Launch-Approval` header or an `approve` field) matching
`SURVEY_LAUNCH_APPROVAL_TOKEN`. With no approval token configured, the gateway can't mark a survey
live at all. A CLI pointed at the gateway (`SURVEY_DEFINITION_SOURCE=http`) forwards the approval it
verified from `--approve`. A `live_version` that doesn't exist is a 400, and an approved mark-live
that names no `live_version` pins the newest saved version.

**Saving definitions.** `PUT /v1/definitions` takes `{"survey": {...}}`, validates it (a 422 lists the
errors) and stores a new version. It never changes what respondents are served: the registry reports
that version as `latest_version`, and `live_version` moves only with an approved registry write.
Survey ids must match `^[A-Za-z0-9_-]+$` (an invalid id is a 400 on registry writes and deletes).

The console still binds `127.0.0.1` by default and is itself unauthenticated; if you host it for a
team (`--host 0.0.0.0`), put it behind your own auth proxy or VPN. The gateway does not provide
organization/tenant scoping or an audit log of mutations — add those at your proxy if you need them.
The **respondent runtime** (`survey-agent serve`) keeps its own direct store on the hot path; the
gateway is the control plane (authoring, lifecycle, reporting reads), not the respondent data plane.

## Hosting the remote MCP connector (chat / cowork on claude.ai)

Remote MCP connectors for web chat clients (for example claude.ai's custom connectors) need an HTTPS
endpoint speaking the MCP transport with **OAuth** in front. Use **Streamable HTTP**.

- **Python path:** run `survey-agent-mcp --transport streamable-http` (the `[mcp]` extra) in the same
  container image on any HTTPS host, and put an OAuth proxy in front (e.g. a managed access proxy or
  oauth2-proxy). survey-agent does not ship OAuth for the connector.
- **Edge path:** implement a thin MCP transport on an edge/worker platform with its own OAuth support
  and have it call the engine over the API gateway.
- **No hosting needed** for desktop/CLI agents: register `survey-agent-mcp` as a local stdio MCP
  server (template: [`.mcp.json.example`](../.mcp.json.example)).

Whatever the transport, the launch gate holds: neither the MCP connector nor the API gateway can mark
a survey live without `SURVEY_LAUNCH_APPROVAL_TOKEN`.

## Quick start: the runtime on Cloud Run

```bash
gcloud run deploy survey-agent \
  --source . --port 8000 --allow-unauthenticated \
  --set-env-vars SURVEY_STORE=mongo,SURVEY_MONGODB_URI="$MONGODB_URI",SURVEY_PUBLIC_BASE_URL="https://<your-run-url>"
```

(`--allow-unauthenticated` makes the respondent pages public, which is the point; it doesn't expose
the console or gateway, which this command doesn't run.) For a throwaway demo you can drop the Mongo
vars and accept that responses reset whenever the instance restarts — fine for kicking the tires, not
for fielding.
