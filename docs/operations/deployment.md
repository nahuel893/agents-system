# Production Deployment and Server Provisioning

This document outlines the operational deployment path for `agents-system`, covering containerized and systemd deployment topologies, automatic migrations on deploy, health check contracts, logging, upgrades, rollbacks, and capacity sizing.

This specification implements issue #12 (tracked as D-036) following the runtime topology decisions in [ADR-001](../architecture/adr-001-runtime-topology.md).

---

## 1. Runtime Topology & Architecture

Per ADR-001, production deployment is organized into two stages:

- **Stage A (Launch — 10 concurrent conversations):** Single application server process. An async event loop comfortably handles I/O-bound LLM requests. The server process hosts:
  - The FastAPI HTTP application (`/health`, `/ready`, `/webhook`, `/v1/*`, `/metrics`).
  - In-process `AuditSink` drainer (background worker writing audit logs to PostgreSQL).
  - In-process `DeferredWebhookWorker` (poll loop processing durable inbound WhatsApp messages from the `outbox_work` table).
  - Two SQLAlchemy async connection pools: operational database and optional read-only BI database.
  - The conversation checkpointer's own psycopg pool on the operational database, and its in-process retention purge (#24, [conversation memory](../platform/conversation-memory.md)).
- **Stage B (Target — 100 concurrent conversations):** Horizontal scaling with multiple server processes behind a reverse proxy/load balancer, utilizing a remote embedding provider (such as OpenAI or a dedicated microservice) to prevent duplicate in-memory BGE-M3 models.

### Process Startup Ordering

Services must start strictly in this dependency sequence:
1. **PostgreSQL:** Starts and reports healthy via `pg_isready`.
2. **Redis:** Starts and reports healthy via `redis-cli ping`. `GET /health` reports it; it no longer holds conversation state, which is in PostgreSQL since #24.
3. **Database Migrations:** One-shot migration job executes `alembic upgrade head`, which also creates the conversation checkpoint tables (revision 007). If migrations fail, the deployment immediately aborts with non-zero exit code. The app refuses to start while the checkpoint schema is missing.
4. **App Server:** Starts only after database, Redis, and migrations are verified healthy and complete.

---

## 2. Resource & Connection Pool Sizing

Database connection pools are created per process. To avoid exhausting PostgreSQL's `max_connections`, operator configuration must obey the sizing rules established in ADR-001.

### Upper Bound Formula

$$\text{total connections} = \text{processes} \times \sum (\text{pool\_size} + \text{max\_overflow}) + \text{concurrent sync processes} < \text{max\_connections} - 3$$

*(3 connections reserved for PostgreSQL superuser).*

### In-Process Operational Pool Constraint

Within every server process, the operational connection pool must satisfy:

$$\text{database\_pool\_size} + \text{database\_max\_overflow} \ge \text{max\_concurrent\_turns} + 2$$

Each admitted conversation turn holds at most one connection during query rounds. The headroom of 2 reserves dedicated connections for the in-process `AuditSink` drainer and `DeferredWebhookWorker`.

### Default Values (Stage A)

| Parameter | Default | Purpose |
|---|---|---|
| `MAX_CONCURRENT_TURNS` | `10` | Process-wide ceiling on concurrent conversational turns |
| `DATABASE_POOL_SIZE` | `5` | Steady-state operational pool size |
| `DATABASE_MAX_OVERFLOW` | `10` | Max operational overflow connections (total capacity 15) |
| `DATABASE_POOL_TIMEOUT_S` | `30` | Connection checkout timeout |
| `BI_POOL_SIZE` / `BI_MAX_OVERFLOW` | `3` / `0` | Dedicated read-only reporting pool |
| `SQL_POOL_SIZE` / `SQL_MAX_OVERFLOW` | `2` / `0` | Dedicated SQL tool pool |
| `CHECKPOINTER_POOL_SIZE` | `5` | Conversation checkpointer pool (psycopg, no overflow). A turn holds one only while its thread is read or saved |
| PostgreSQL `max_connections` | `100` | Minimum recommended PostgreSQL configuration |

---

## 3. Deployment Methods

We provide two deployment options:
1. **Docker Compose (`docker-compose.prod.yml`):** Recommended for containerized infrastructure and automated CI/CD environments.
2. **Systemd Units (`deploy/systemd/`):** Recommended for bare-metal Linux VMs running services directly under host OS management.

### Option A: Docker Compose Deployment

The stack is defined in `docker-compose.prod.yml` with four coordinated services: `postgres`, `redis`, `migrate`, and `app`.

#### Environment Injection & Secrets
Configuration and secrets are loaded from `.env.prod` (or a custom path via `ENV_FILE`, default `${ENV_FILE:-.env.prod}`) and injected into both the `migrate` and `app` services using `env_file`.

#### Security Baseline
All external ports are bound to `127.0.0.1`. Never expose database or application ports directly to `0.0.0.0`. A host reverse proxy (Nginx, Caddy, Cloudflare Tunnel, AWS ALB) should terminate TLS and route external traffic.

#### Deployment Steps

1. **Configure Environment:**
   ```bash
   cp .env.example .env.prod
   chmod 600 .env.prod
   # Populate secrets (POSTGRES_PASSWORD, META_WEBHOOK_SECRET, LLM API keys)
   ```

2. **Validate Compose Configuration:**
   ```bash
   docker compose -f docker-compose.prod.yml config
   ```

3. **Build and Deploy Stack:**
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   ```

4. **Verify Deployment Health:**
   ```bash
   docker compose -f docker-compose.prod.yml ps
   scripts/healthcheck.sh all
   ```

### Option B: Systemd Unit Deployment

Systemd units are located in `deploy/systemd/`:
- `agents-system-migrate.service`: A `Type=oneshot` unit that executes database migrations before the application starts.
- `agents-system-app.service`: A `Type=simple` unit managing the Uvicorn application server with automatic restarts on crash.

Both units execute as the dedicated unprivileged user and group `agents-system`, set `PATH` to include `/opt/agents-system/.venv/bin`, and load environment variables from `EnvironmentFile=/etc/agents-system/agents-system.env` (without the `-` prefix, ensuring missing configuration files fail fast at boot).

#### Installation Steps

1. **Create Service User and Directories:**
   ```bash
   sudo useradd -r -s /bin/false -d /opt/agents-system agents-system
   sudo mkdir -p /opt/agents-system /etc/agents-system /var/log/agents-system
   sudo chown -R agents-system:agents-system /opt/agents-system /var/log/agents-system
   ```

2. **Install Code and Virtualenv:**
   ```bash
   sudo -u agents-system uv sync --frozen --no-dev --extra all
   ```
   A plain install is the library core (issue #188); the served app needs its
   extras, and `all` is every one of them. See "Extras" in
   `docs/platform/library-usage.md`.

3. **Configure Environment File:**
   ```bash
   sudo cp deploy/systemd/agents-system.env.example /etc/agents-system/agents-system.env
   sudo chown root:agents-system /etc/agents-system/agents-system.env
   sudo chmod 640 /etc/agents-system/agents-system.env
   # Edit /etc/agents-system/agents-system.env with production credentials
   ```

4. **Install and Enable Units:**
   ```bash
   sudo cp deploy/systemd/agents-system-migrate.service /etc/systemd/system/
   sudo cp deploy/systemd/agents-system-app.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable agents-system-app.service
   sudo systemctl start agents-system-app.service
   ```

### Application Entrypoint & Tool Registry Resolution (`REGISTRY_FACTORY`)

Production deployments run the Uvicorn ASGI server against `agents_system.server:app`. The application factory resolves connector tools as follows:

1. **Zero Registered Agents:** If `AGENT_REGISTRATIONS` is empty or unset, the application boots cleanly with an empty runtime cache (serving `/health` and webhook intake).
2. **Registered Agents Require `REGISTRY_FACTORY`:** When `AGENT_REGISTRATIONS` (or `create_app(agents=...)`) specifies one or more agents, the deployment **must** configure `REGISTRY_FACTORY` pointing to a callable (format: `module.submodule:callable_name`) that returns a `ToolRegistry` equipping every tool those agents declare.
3. **Fail-Closed Default Registry:** When `REGISTRY_FACTORY` is unset, `default_registry_factory` is used. Because `default_registry_factory` only registers stub platform tools (`order_writer`, `knowledge_retrieval`, `conversation_summarizer`, `escalation_notifier`, `run_report`) and cannot equip predefined roles requiring role-specific tools (such as `catalog_search`, `client_lookup`, or `message_sender`), startup aborts immediately with a clear `DefinitionError` naming the missing tools.
4. **Reference Implementation:** Check `examples/demo/app.py` for how a real deployment adapts connectors to its storage and language model via `build_live_registry_factory`.

---

## 4. Health Checks & Restart Policies

Per issue #109 and ADR-001, health monitoring uses two distinct endpoints with strict operational semantics:

### `/health` — Liveness Probe
- **Contract:** Always returns HTTP 200 with structured component diagnostics as long as the Python process and event loop are responsive.
- **Consumer:** Process managers (Docker daemon healthcheck in `docker-compose.prod.yml`, systemd watchdog, Kubernetes liveness probes).
- **Restart Guarantee:** A degraded or unreachable external model provider (Anthropic, Groq, OpenAI) degrades the reported status inside the JSON payload, but **never** returns an HTTP 5xx error. Supervisors will not restart a healthy application when upstream LLM APIs experience transient outages.

### `/ready` — Readiness Probe
- **Contract:**
  - Returns HTTP 200 when PostgreSQL, Redis, and model provider are healthy and there is no unserviced backlog.
  - Returns HTTP 503 when any core dependency is down, when the model provider is unreachable, or when outbox work has expired/abandoned leases without a running worker.
- **Consumer:** Load balancers, reverse proxies (e.g. Nginx, Traefik, AWS ALB), and Kubernetes readiness probes for traffic routing and shaping.
- **Traffic Guarantee:** Traffic routing is paused during dependency degradation without killing or restarting the process. `/ready` belongs exclusively to routing layers, never to container or process supervisors.

### Healthcheck Script
Run the automated probe script:
```bash
scripts/healthcheck.sh readiness   # Checks /ready (exit 0 on 200, exit 1 on 503)
scripts/healthcheck.sh liveness    # Checks /health (exit 0 on 200, exit 1 on connection error)
scripts/healthcheck.sh all         # Checks both endpoints
```

---

## 5. Migrations on Deploy

Database migrations are managed via Alembic and run automatically on every deploy before the application starts.

- **URL Resolution:** `DATABASE_URL` is read directly from the environment by `alembic/env.py`. Versioned `alembic.ini` contains no credentials.
- **Dry-run Verification:**
  ```bash
  DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/dbname" uv run alembic upgrade head --sql
  ```
- **Automated Deploy Execution:**
  In Docker Compose, the `migrate` service runs `alembic upgrade head`. The `app` service declares:
  ```yaml
  depends_on:
    migrate:
      condition: service_completed_successfully
  ```
  If a migration script encounters an error or lock conflict, the container exits with non-zero status and Docker Compose halts the deployment before starting `app`.
- **The checkpoint tables (revision 007):** they belong to `langgraph-checkpoint-postgres`; the revision runs that library's own migrations instead of copying their DDL. Upgrading from the Redis checkpointer, and what the new retention settings mean for personal data, is described in [conversation memory](../platform/conversation-memory.md#upgrading-from-the-redis-checkpointer).

---

## 6. Upgrades & Rollbacks

### Zero-Downtime Upgrade Sequence

1. **Pull updated image or repository release tag:**
   ```bash
   git fetch origin && git checkout vX.Y.Z
   ```
2. **Execute Migrations:**
   Migrations must be backward-compatible (expand-contract pattern). Schema changes should not break the running version of the application before the new version starts.
3. **Restart / Recreate Application Service:**
   ```bash
   # Docker Compose
   docker compose -f docker-compose.prod.yml up -d --no-deps --build app

   # Systemd
   sudo systemctl restart agents-system-app.service
   ```
4. **Verify Health:**
   ```bash
   scripts/healthcheck.sh readiness
   ```

### Rollback Procedure

If an upgrade introduces regressions:

1. **Roll Back Application Code / Container:**
   ```bash
   # Docker Compose: revert to previous image tag or commit
   docker compose -f docker-compose.prod.yml up -d --no-deps app

   # Systemd: checkout previous release tag and restart
   git checkout <previous-tag>
   sudo systemctl restart agents-system-app.service
   ```
2. **Roll Back Database Schema (if required):**
   ```bash
   uv run alembic downgrade -1
   # Or downgrade to specific revision:
   uv run alembic downgrade <revision_id>
   ```
   *Note: Only downgrade if the migration was written as reversible and did not perform destructive data drops.* Downgrading below revision 007 drops every conversation thread.

---

## 7. Logging & Retention

- **Format:** Structured JSON lines via `structlog`. Every log entry includes ISO timestamps, log level, event name, and contextual attributes (`request_id`, `thread_id`, `correlation_id`).
- **Destination:** Standard output (`stdout`) / standard error (`stderr`), captured by Docker logging driver or systemd `journald`.
- **No Secrets in Logs:** Secrets, authentication tokens, and user PII are redacted by default deny policies (`Redactor` and `RequestIdMiddleware`).
- **Retention Policy:**
  - **Docker Compose:** Configured with `json-file` driver, rotating at `100MB` per file with `5` files retained (max 500MB per service):
    ```yaml
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
        max-file: "5"
    ```
  - **Systemd / Journald:** Managed via `/etc/systemd/journald.conf`:
    ```ini
    [Journal]
    SystemMaxUse=2G
    SystemKeepFree=1G
    MaxRetentionSec=30day
    ```
