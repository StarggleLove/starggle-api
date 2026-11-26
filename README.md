# StarggleAPI

**English** | [简体中文](README.zh-CN.md)

A lightweight gateway that puts a single OpenAI-compatible API in front of many LLM providers.
Configure providers, models, clusters, and request parameters from one admin console, and track
usage for every call. It absorbs the call-layer plumbing — protocol differences, retries,
failover, quotas, usage, and logs — so your services only need an OpenAI-compatible client.

Model clusters keep realtime calls isolated from background batch jobs, so night-time batch
parsing or report generation cannot starve core traffic.

> [!WARNING]
> The default admin account is `admin / admin` and `JWT_SECRET_KEY` is a placeholder.
> **Use these defaults for local evaluation only.** Before exposing the service to a LAN or the
> internet, change `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `JWT_SECRET_KEY`.
> Never commit `.env`, `data/`, database files, or logs to the repository.

## Who it's for

Built for internal backends and small teams that call LLMs from their own services — business
APIs, background jobs, report generation, or multiple product lines that need separate quotas
and failure isolation.

- **Good fit**: one or a few operators, internal traffic, on-prem/private deployments,
  OpenAI-compatible clients, SQLite on a single host, or MySQL when you need multiple instances
- **Not a fit**: public multi-tenant API resale, per-user recharge/billing, org-wide RBAC with
  department hierarchies and SSO, or agent chat-log analytics. Use new-api, one-api, or LiteLLM
  for those.

Typical use cases:

- **ERP / inventory / CRM add-ons**: document and contract summaries, natural-language inventory
  or receivables queries, batch report analysis
- **Internal workflow engines**: a model call at each step, with cost caps and automatic failover
- **Several internal tools sharing one provider setup**, isolated by project and cluster

The gateway owns call-layer concerns: provider protocols, retries, circuit breaking, quotas,
usage accounting, and logging. Your business code still owns prompt design, result validation,
and business-level fallbacks.

## Features

- **One API for many providers**: OpenAI-compatible `POST /chat/completions`, with streaming (SSE) and non-streaming responses
- **Centralized provider config**: base URLs and API keys live at the provider level; models reference a provider
- **Model clusters and round-robin**: models are grouped into nine clusters (`async1..async9`) with automatic round-robin inside each cluster. Use separate clusters to isolate production calls, background batches, and reports so batch jobs cannot starve core traffic
- **Projects and API keys**: issue `sk-gw-...` tokens per project, with revocation, rotation, and model allow-lists
- **Project quotas**: daily/monthly caps on calls, tokens, and cost
- **Channel health**: success rate, latency, circuit breaking/cool-down, and automatic failover within a cluster
- **Cost-aware routing**: each cluster can use round-robin, cost-first, or latency-first routing
- **Provider parameter adaptation**: built-in handling for reasoning parameters and protocol differences across DeepSeek, GLM, MiniMax, Claude, Gemini, Qwen, and others
- **Per-model parameter overrides**: override `temperature`, `top_p`, `top_k`, and `max_tokens` per model
- **Usage analytics**: token usage and call records, grouped by project, model, feature code, or user
- **Cost estimation**: set input/output prices per model to estimate cost from call records (OpenRouter's actual upstream charge is used when available)
- **Request tracing**: pass through or generate `X-Request-Id`; response headers expose the actual model, cluster, and retry count
- **Admin console**: providers, models, clusters, projects, keys, channel health, and integration docs in one place
- **Built-in chat UI**: `/chat` serves an offline Chatbox-style client with local conversation history, streaming, stop, regenerate, and JSON import/export
- **Payload log switch**: full LLM request/response logs are on by default and can be turned off under "Channel Health"; logs may contain user prompts
- **Bilingual UI**: English by default, with a one-click switch to Chinese in the bottom-right corner
- **Role-based admin accounts**: four built-in roles (`admin` / `operator` / `developer` / `viewer`) defined in environment variables, JWT login, no extra user system required
- **Zero-config storage**: SQLite by default, no Redis or message queue required; switch to MySQL for multi-instance deployments

## How it compares

StarggleAPI is not a drop-in replacement for LiteLLM or new-api. It targets small self-hosted
deployments that want a compact, SQLite-first gateway with cluster routing, channel health, and
per-project quotas, without a multi-tenant user system.

| | StarggleAPI | [LiteLLM](https://github.com/BerriAI/litellm) | [new-api](https://github.com/QuantumNous/new-api) | [one-api](https://github.com/songquanpeng/one-api) |
|---|---|---|---|---|
| License | Apache-2.0 | MIT | AGPL-3.0 | MIT |
| Stack | Python / FastAPI | Python | Go / React | Go |
| Admin model | env-defined accounts with four roles | multi-user, teams, virtual keys | multi-user, groups, OAuth | multi-user, groups |
| Providers | preset list; any OpenAI-compatible endpoint can be added | 100+ integrations | 40+ providers | many OpenAI-compatible providers |
| Routing | nine clusters (`async1..async9`); round-robin, cost-first, latency-first | weighted, least-busy, latency, usage, cost, fallback chains | channel groups with priority and weight | channel groups, priority, load balancing |
| Channel health | success rate, latency, circuit breaker, half-open probe | retries, cooldowns, fallbacks | channel test, auto-disable | channel test, auto-disable |
| Quotas | per project: daily/monthly calls, tokens, cost | per key/user/team budgets | user and token quotas with recharge | user and token quotas with redemption |
| Storage | SQLite (default) / MySQL | PostgreSQL (+ Redis) | SQLite / MySQL / PostgreSQL (+ Redis) | SQLite / MySQL / PostgreSQL (+ Redis) |

### Limitations

- No virtual-model layer: a model maps to real provider rows, with no prompt/model composition
- Admin accounts and the four roles are defined in env vars (`ADMIN_USERS`); no sign-up, OAuth/SSO, department hierarchies, or per-end-user accounts
- Routing is round-robin, cost-first, or latency-first per cluster; no weights, priority tiers, or quality-based model selection
- No built-in alerting or Prometheus export; use the health dashboard and your log pipeline
- No guardrails or content-policy layer
- Provider coverage is a preset list rather than 100+ integrations
- SQLite is single-process; use MySQL for multiple workers or instances
- Multi-instance state lives in MySQL; there is no Redis-backed routing or rate-limit cache

*Comparison reflects public documentation as of November 2026. Corrections are welcome via an issue.*

## Screenshots

![Usage dashboard](docs/screenshots/admin-usage.png)

![Models and channel health](docs/screenshots/admin-models.png)

![Built-in chat](docs/screenshots/chat.png)

## Prerequisites

Python **3.10+** is required for the one-click scripts and the manual setup. The Docker path
does not need a local Python installation.

**Windows**

Download from <https://www.python.org/downloads/windows/> and enable **Add python.exe to PATH**
in the installer. Then open a new terminal and check:

```bash
python --version        # or: py -3 --version
```

**macOS**

Install from <https://www.python.org/downloads/macos/>, or with Homebrew:

```bash
brew install python
python3 --version
```

**Linux (Debian / Ubuntu)**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version
```

If your distribution ships a Python older than 3.10, install a newer release from
<https://www.python.org/downloads/> or with `pyenv`.

For Docker, install Docker Desktop (Windows/macOS) or Docker Engine with Compose v2 instead.

## Quickest start: one-click launch

Requires Python 3.10+ — see [Prerequisites](#prerequisites).

- **Windows**: double-click `start.bat`
- **macOS / Linux**: `bash start.sh` (works even if a ZIP download lost the executable bit)

The script creates a virtual environment, installs dependencies, generates `.env`, starts the
service, and prints the admin URL (on Windows it also opens the browser). Default URL:
<http://127.0.0.1:9010/>. Default account: `admin / admin`.

> The one-click script listens on `127.0.0.1` by default. For LAN or server access, run
> `HOST=0.0.0.0 bash start.sh` and open port 9010 in your firewall or cloud security group.

> The default account is intended for local use only. Before serving a LAN or the internet,
> change `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `JWT_SECRET_KEY` in `.env`.

## Quick start (Docker)

```bash
cp .env.example .env
# edit the admin credentials and JWT_SECRET_KEY in .env
docker compose up -d --build
```

Open <http://localhost:9010/> and sign in with the admin account from `.env`.

SQLite is used by default. The database file is persisted in the `gateway_data` volume, so no
external database is required.

## Run manually (local development or a Linux server)

```bash
git clone https://github.com/StarggleLove/starggle-api.git
cd starggle-api

python3 -m venv .venv            # Windows: py -3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

uvicorn main:app --host 0.0.0.0 --port 9010
```

Tables are created automatically on startup. The database defaults to `data/gateway.db`.
On a server, `--host 0.0.0.0` is required for outside access; open port 9010 in your firewall
or cloud security group.

> SQLite fits single-process deployments. Do not run multiple uvicorn workers or share one
> SQLite file across multiple instances. Switch to MySQL if you need multiple instances or
> heavy concurrent writes.

## Tests

The tests depend only on the Python standard library and use a separate temporary SQLite file,
so they never touch `data/gateway.db`:

```bash
python -m unittest discover -s tests -v
```

You can also install `requirements-dev.txt` and run them with pytest.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `DB_BACKEND` | `sqlite` or `mysql` | `sqlite` |
| `SQLITE_PATH` | SQLite data file path | `data/gateway.db` |
| `DATABASE_URL` | Explicit SQLAlchemy async URL (highest priority) | empty |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | MySQL connection settings (used when `DB_BACKEND=mysql`) | localhost / 3306 / root / empty / `llm_gateway` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Legacy single-admin credentials (stored in plaintext in the env, fixed to the `admin` role) | `admin` / `admin` |
| `ADMIN_USERS` | Multi-account + roles as JSON; each account is `admin` / `operator` / `developer` / `viewer` | empty |
| `JWT_SECRET_KEY` | JWT signing key; must be changed in production | code default `dev-only-change-me`; the example file uses a placeholder |
| `HOST` | Bind address used by `start.sh` / `start.bat` | `127.0.0.1` |
| `PORT` | Port used by `start.sh` / `start.bat` and `python main.py`; Docker uses the compose port mapping | `9010` |
| `GATEWAY_DEBUG` | Set to `1` to include debug details in error responses | `0` |
| `GATEWAY_TOKEN_REQUIRED` | Set to `1` to require an `sk-gw-` project token on every call | `0` |
| `USAGE_RETENTION_DAYS` | Days to keep raw call records; `0` disables cleanup | `90` |
| `CORS_ORIGINS` | Allowed origins for the admin console, comma-separated | empty (same-origin is enough) |

### Admin accounts and roles

There is no user table; accounts come from environment variables. Two forms can be combined:

```bash
# Legacy single admin, fixed to the admin role
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
# Optional multi-account + roles (JSON). A plain string value means "password only"
ADMIN_USERS={"alice": {"password": "s3cret", "role": "admin"}, "bob": {"password": "s3cret", "role": "developer"}}
JWT_SECRET_KEY=change-me-to-a-long-random-string
```

Built-in roles:

| Role | Permissions |
|---|---|
| `admin` | Everything, including deleting providers and viewing upstream API keys |
| `operator` | Read/write models, providers, projects, channels and routing; cannot delete providers; can view upstream keys |
| `developer` | Read-only + create/rotate/revoke project API tokens |
| `viewer` | Read-only; upstream API keys are never returned |

- Changing accounts, passwords, or roles requires a **service restart**; roles are re-resolved from the account table on every request, so a restart takes effect immediately even for already-issued JWTs
- Removing an account makes its tokens fail immediately (401)
- Changing `JWT_SECRET_KEY` invalidates every issued token immediately
- There is no sign-up or password recovery; manage accounts in the env. To plug in your own user system, replace the auth layer as described in "Integrating your own user system"
- The default `admin / admin` is for local use only; change it before exposing the service to a LAN or the internet

### SQLite data and backups

The default database is a single file, `data/gateway.db`, created on first startup.

```bash
# Stop the service, then copy the file
cp data/gateway.db data/gateway.db.bak
```

WAL mode also creates `gateway.db-wal` / `gateway.db-shm`. To back up while the service is running:

```bash
sqlite3 data/gateway.db ".backup 'data/gateway.db.bak'"
```

With Docker, the data lives in the `gateway_data` volume. Do not delete that volume.

### Switching to MySQL

Create the database first (tables are created automatically on startup):

```sql
CREATE DATABASE IF NOT EXISTS llm_gateway
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Then configure `.env`:

```bash
DB_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your-password
MYSQL_DATABASE=llm_gateway
```

In MySQL mode the app runs `app/db/schema.sql` and idempotent migrations on startup.
There is no automatic data migration between SQLite and MySQL; export/import manually before switching.

### Security and deployment notes

- Change `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `JWT_SECRET_KEY` before any public deployment
- Put the service behind an HTTPS reverse proxy (Nginx/Caddy) and rate-limit login at the proxy
- Run only one process in SQLite mode; do not use multiple workers or share the file across instances
- Full LLM request/response payloads are **written to the server log by default**. Turn them off under "Channel Health → LLM payload logs". Logs may contain user prompts and should be protected accordingly
- Project tokens are stored as SHA-256 hashes only; the full secret is returned once at creation/rotation
- Gateway tokens are never forwarded upstream; upstream keys always come from server-side config
- Channel health stores only error types/HTTP status codes, never upstream error bodies, to avoid storing prompt fragments

### Integrating your own user system

The open-source edition keeps accounts in the environment: `app/api/v1/controller/auth_controller.py`
authenticates against `ADMIN_USERNAME` / `ADMIN_PASSWORD` and optional `ADMIN_USERS`, then issues a JWT
whose role is resolved by `app/utils/rbac.py`. It never queries a user table.

To plug in your own user table, OAuth, or SSO, replace these places:

- `auth_controller.py` / `admin_auth.authenticate`: validate your own users in the login endpoint
- `admin_auth.verify_admin_token`: return an `AdminIdentity` (username + role) from your token
- `app/utils/rbac.py`: map your roles onto the four built-in roles, or extend the permission matrix

Admin endpoints are guarded by `Depends(verify_admin_token)` plus a per-route
`Depends(require_permission(...))`, so replacing this layer is enough.

## Calling the gateway

Use the OpenAI-compatible endpoint `/v1/chat/completions`:

```bash
curl -X POST "http://localhost:9010/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gw-your-project-token" \
  -H "X-Async-Type: async1" \
  -H "X-Feature-Code: role_reply" \
  -d '{
    "model": "",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

Compatible aliases (equivalent):

- `POST /router/call-manage/chat/completions`
- `POST /api/v1/call-manage/chat/completions`

OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9010/v1",
    api_key="sk-gw-your-project-token",
    default_headers={"X-Async-Type": "async1"},
)
resp = client.chat.completions.create(
    model="",
    messages=[{"role": "user", "content": "Hello"}],
)
```

Model list: `GET /v1/models` returns active text models; when an `sk-gw-` project token is
provided, the list is filtered by the token's model allow-list. The `x_clusters` field in the
response can be used directly as `X-Async-Type`.

Request headers:

| Header | Value | Description |
|---|---|---|
| `Authorization` | `Bearer sk-gw-...` | Project token; the gateway validates the project, quota, and model scope, and never forwards it upstream |
| `X-Async-Type` | `async1`..`async9` | Model cluster; defaults to `async1` for the main chain and `async2` for async chains |
| `X-Purpose` | `main` or any value starting with `async` | Async detection, logging, and usage attribution; does not affect cluster selection |
| `X-Thinking` | `1` / `0` | Thinking switch; only applies when the model's thinking mode is "follow request" |
| `X-Response-Format` | `json_object` | Request JSON output |
| `X-User-Id` | Caller identifier | Used for call records and usage attribution |
| `X-Feature-Code` | e.g. `role_reply` | Feature code for usage breakdowns; omitted calls are recorded as `uncategorized` |
| `X-Request-Id` | optional, ≤64 chars | Request tracing; generated by the gateway when omitted |

> An `Authorization` value without the `sk-gw-` prefix is still handled with legacy semantics:
> the main chain may override the upstream key, while async chains always use the server-side key.
> Setting `GATEWAY_TOKEN_REQUIRED=1` requires project tokens.

## Built-in chat UI

Open <http://127.0.0.1:9010/chat>, or choose "Chat" in the admin console sidebar. It is a lightweight
Chatbox-style client that calls the gateway's `/v1/chat/completions` directly:

- Conversations and messages live in browser IndexedDB (falling back to localStorage) and are never written to the gateway database
- Supports streaming output, stop, regenerate, model switching, system prompts, and JSON import/export
- The local open-source mode allows an empty API key; when `GATEWAY_TOKEN_REQUIRED=1` or project quotas are used, enter an `sk-gw-...` token under Settings
- For a full-featured client, Chatbox / Open WebUI / Cherry Studio can use `base_url=http://host:9010/v1` with a project token

## Payload logging

Full LLM payloads are written to the server log by default. Toggle it under
**Channel Health → LLM payload logs**.

When on, the gateway logs:

- the request body sent upstream
- the full JSON response for non-streaming calls
- the reconstructed `content`, `reasoning_content`, and `usage` after a streaming call

When off, only summary lines remain: status, latency, byte counts, and token usage.

The setting is stored in the database, not `.env`, and applies immediately to the current process;
other workers refresh within 15 seconds. Logs may contain user prompts — protect them accordingly.

## Projects and quotas

1. Open the admin console → "Projects & Keys" → create a project
2. Create a token in the project and copy the `sk-gw-...` value. The full secret is shown only once
3. Call with `Authorization: Bearer sk-gw-...`

Each project supports six limits: daily/monthly × calls, tokens, and cost. Rules:

- Empty = unlimited; `0` = calls blocked
- Call counts are checked before the request; tokens/cost are accumulated after the response, so they are soft limits (at most one request over)
- Only successful requests are counted; internal gateway retries count as one logical call
- Cost only accumulates records in the same currency as the project; no FX conversion
- Quota violations return `429`, e.g. `daily_call_quota_exceeded`
- Tokens can be restricted to an allow-list of models, with `*` wildcards; disabling a project keeps its historical usage

Successful responses include `X-Request-Id`, `X-Gateway-Model`, `X-Gateway-Cluster`, and
`X-Gateway-Retry-Count`.

## Channel health and cost-based routing

Each model row in "Models" is treated as a channel. The gateway records today's successes/failures,
average latency (TTFB for streaming), consecutive failures, and the last error, and shows them under
"Channel Health".

- Once consecutive failures hit the threshold, the channel is circuit-broken and skipped during cool-down
- After cool-down it probes half-open: success restores it, failure breaks it again
- Within a cluster, automatic failover for connection errors, timeouts, 429, 5xx, and 401/403
- 400/404/422 are request/model problems and do not trigger failover
- If every channel is broken, the gateway still tries them to avoid a total outage
- Streaming requests retry before output starts; once output begins, the channel is not switched

Each cluster supports three strategies:

| Strategy | Description |
|---|---|
| `round_robin` | Round-robin (default, backward compatible) |
| `cost_first` | Estimate cost from input/output prices, cheapest first; models without prices go last |
| `latency_first` | Lowest average latency today first |

Cost estimation uses the model's `input_price_per_1m` / `output_price_per_1m`; the output ratio is
configurable in the routing config (default 0.3). Missing prices or latency data fall back to round-robin.

## Project layout

```
app/
  api/v1/controller/   Admin, model, project, health, and call-forwarding endpoints
  config/              Database and runtime config
  db/schema.sql        MySQL DDL (SQLite tables are created by the ORM)
  models/              Provider, model, usage, project, token, health, routing ORM models
  services/            Model selection/routing, usage, projects, channel health
  utils/               JWT, snowflake IDs, unified responses, OpenAI error format
templates/model_manage.html   Admin console
templates/chat.html           Built-in chat UI
static/chat/                  Chat UI CSS / JS
static/i18n.js                UI translations (English default, Chinese source)
tests/                        Standard-library unittest suite
providers.txt                 Provider dropdown list (domain, name, notes)
main.py                       FastAPI entry point
```

## License

Apache-2.0
