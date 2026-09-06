# Relay issue operations control plane

Relay is a standalone control plane for a Devin-powered issue intake,
reproduction, and fix workflow targeting only `exloong/superset`. It includes:

- an operational analytics dashboard;
- a visual lifecycle designer;
- an issue workbench with reporter guidance, evidence, and owner gates;
- configurable triage, inactivity, safety, and agent-instruction policies;
- a FastAPI lifecycle API with PostgreSQL persistence;
- a durable worker with bounded retries and revision checks;
- signed GitHub webhook ingress;
- fake adapters for credential-free dry runs.

The default Docker configuration is an explicit demo mode. It never writes to
GitHub, starts live Devin sessions, merges pull requests, or closes issues.

## Implementation design

- [Issue automation platform architecture](docs/architecture/issue-automation-platform.md)
- [Parallel implementation and approval plan](docs/plans/parallel-implementation-plan.md)

## Research

- [Apache Superset issue intake and bug resolution report](public/reports/apache-superset/issue-intake-2025-09-05-to-2026-09-04.html)
  covers 1,030 public issues created during the 365 complete days ending
  September 4, 2026. It documents the measured GitHub data, classification
  proxies, lifecycle timing, backlog age, and automation opportunities that
  informed the mockup.

## Run with Docker

```bash
docker compose up --build
```

Open `http://localhost:4173`.

The stack starts:

- `web`: React application and Nginx reverse proxy;
- `api`: lifecycle API and programmatic Alembic migrations;
- `worker`: durable job consumer and fake external effects;
- `postgres`: persistent control-plane state.

The API health endpoints are available through the web origin at
`/api/v1/health` and `/api/v1/ready`. `ready` reports database migration and
worker-heartbeat status.

To use another host port:

```bash
APP_PORT=8080 docker compose up --build
```

State is retained in the `relay_postgres` Docker volume. To stop without
deleting state:

```bash
docker compose down
```

To delete local demonstration state:

```bash
docker compose down --volumes
```

## Dry-run flow

The browser uses same-origin `/api/v1` requests. Demo mode derives a trusted
local operator, reporter, or owner actor server-side for each command; the
browser never submits identity or role fields.

1. Create a dry run with `POST /api/v1/dry-runs`.
2. The worker classifies it and asks for deterministic reproduction steps.
3. Submit the requested context in the issue workbench.
4. The fake reproduction session publishes evidence and releases its workspace.
5. A human owner confirms the bug and authorizes a scoped fix.
6. The fake fix opens a Superset PR record, runs fake Devin Review, and resolves
   a deterministic CODEOWNERS route.
7. Human approval is recorded, but Relay does not merge or close the issue.

## GitHub webhook ingress

Configure the same secret in the GitHub webhook and the API:

```bash
GITHUB_WEBHOOK_SECRET='replace-with-a-random-secret' docker compose up --build
```

Use the webhook URL:

```text
https://<relay-host>/api/v1/webhooks/github
```

Relay verifies the HMAC-SHA256 signature over the raw body, enforces the
`exloong/superset` repository scope, validates the event envelope, and persists
GitHub delivery IDs through the lifecycle event table. Supported lifecycle
events include issue open/reopen, reporter comments, PR open/synchronize/merge,
and owner reviews. Unsupported actions are acknowledged and ignored.

## Live integration configuration

The typed GitHub, Devin v3 session, and Devin Review adapters are implemented,
but the Docker worker deliberately refuses non-demo execution until a
credential-backed runtime transport is configured. This prevents a deployment
from silently inheriting organization secrets or performing external writes.
Live mode must provide dedicated, minimally scoped credentials and preserve all
human gates described in the architecture document.

## Local development

```bash
npm install
npm run dev
```

Backend:

```bash
cd backend
uv sync --extra dev
uv run uvicorn app.api.app:default_app --factory --reload
uv run python -m app.runtime.worker
```

## Validation

```bash
npm run typecheck
npm run build
npm run test:fixtures

cd backend
uv run pytest
uv run ruff check app tests
uv run mypy

docker compose up --build --wait
curl --fail http://localhost:4173/api/v1/ready
```
