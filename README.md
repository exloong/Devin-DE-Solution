# Relay issue operations control plane

Relay is a standalone control plane for a human-governed, Devin-powered issue
workflow. It turns GitHub issue events into a durable process for triage,
reporter follow-up, reproduction, fix authorization, implementation, review,
and owner approval.

This repository contains the Relay product itself: the React operator
dashboard, FastAPI API, durable worker, PostgreSQL persistence, GitHub and
Devin integrations, and a credential-free local demo. The only supported
target repository is currently `exloong/superset`.

## What Relay does

- Accepts and verifies signed GitHub issue, comment, pull-request, and review
  events from `exloong/superset`.
- Stores issue revisions, lifecycle transitions, jobs, conversations,
  evidence, sessions, pull requests, reviews, approvals, and audit history.
- Runs deterministic policy separately from AI judgment.
- Starts bounded Devin reproduction and fix sessions against an immutable
  Superset commit.
- Asks reporters for only the evidence still needed to reproduce an issue.
- Routes reproduced bugs and pull requests to human owners for explicit
  decisions.
- Exposes live operational state, session details, evidence, and owner gates in
  the dashboard.
- Recovers safely from duplicate webhooks, retries, restarts, stale jobs, and
  synchronized pull-request heads.

## What Relay does not do

- It does not support repositories other than `exloong/superset`.
- It does not automatically merge pull requests or close confirmed bugs.
- It does not treat Devin or Devin Review as a substitute for human approval.
- It does not execute reporter-provided commands or download untrusted
  attachments.
- It does not publish security-sensitive reports or private Devin
  session/desktop links into public GitHub comments.
- It does not keep an agent workspace alive while waiting for a reporter or
  owner.
- It does not treat text matches, model confidence, or a passing review as
  authoritative proof that a bug was reproduced or fixed.
- It does not put GitHub, Devin, Review, or webhook credentials in the browser
  bundle or lifecycle records.

## How it works

```text
GitHub webhook
      │
      ▼
Relay API ── validates signature, repository, delivery, and command
      │
      ▼
PostgreSQL ── stores lifecycle state, audit history, and durable jobs
      │
      ▼
Relay worker ── claims jobs and calls GitHub, Devin, and Devin Review
      │
      ▼
Human gates ── authorize code work and approve the exact PR head
      │
      ▼
React dashboard ── reads the API and exposes actions to authenticated operators
```

The normal lifecycle is:

```text
new → triage → awaiting_reporter → reproducing → needs_owner_decision
    → fix_authorized → fixing → pr_open → awaiting_owner → completed
```

There are also explicit holding or terminal states for environment blockers,
requested changes, duplicates, non-bugs, unsupported reports, and inactivity.

Each event is handled as follows:

1. GitHub sends a signed event to `/api/v1/webhooks/github`.
2. The API verifies the HMAC signature, delivery ID, event shape, and repository
   scope, then records the event and its resulting transition atomically.
3. The transition creates durable work for the worker; duplicate deliveries and
   stale commands cannot advance the issue twice.
4. The worker claims jobs and uses either deterministic demo adapters or live
   GitHub, Devin Sessions, Devin Review, and CODEOWNERS integrations.
5. Structured results are accepted only for the issue revision and immutable
   commit that created the task.
6. A human owner must confirm the bug before a fix session starts. Review and
   approval are bound to the exact PR head SHA, so a new push invalidates
   evidence for the previous head.

### The core flow and the two Devin trigger points

The operator UI shows a single linear path. Only two of its steps launch a
Devin session; everything else is deterministic policy or a human decision.

| Step | Actor | What happens |
| --- | --- | --- |
| Intake & classify | Workflow controller (deterministic) | Verifies the webhook signature, applies the controller gate (repository is `exloong/superset`, issue is open, optional trusted label/actor), and classifies the report. **No Devin session is launched here.** |
| Reproduce safely | Devin reproducer (`kind = reproduction`) | **Auto-launched** by the worker once triage marks the report a likely defect with context completeness ≥ 80%. Builds an isolated fixture, runs control/failure cases, drafts a regression test. |
| Confirm the bug | Component owner (human gate) | Reviews the evidence pack and explicitly confirms or rejects the bug. |
| Prepare the fix | Devin coding agent (`kind = fix`) | Launched **only** after the owner's `confirm_bug` decision (`POST /api/v1/issues/{id}/decisions`). Implements the smallest fix with a regression test and opens a PR. |
| Review & approve | Owners / Devin Review | PR review; Relay never merges automatically. |

`GET /api/v1/sessions?kind=reproduction|fix` lists sessions of either kind,
each with status, linked issue, timestamps, and the external Devin URL when
one exists.

### How Devin sessions are launched: Devin Automations

Relay never calls the Sessions API to start reproduction or fix work; Devin
Automations are the only launcher. At startup the worker uses the
[Devin Automations API](https://docs.devin.ai/api-reference/v3/automations/post-organizations-automations)
to find, or create once, exactly two organization-scoped automations,
identified by their metadata (`relay_kind=reproduction|fix`,
`relay_repo=exloong/superset`). The definitions live in Devin: the worker does
not overwrite a name, prompt, or enabled flag that an operator changed.

| Automation | Trigger | Action |
| --- | --- | --- |
| `Relay reproduction · exloong/superset` | `webhook:incoming` | `start_session` with the reproducer instructions |
| `Relay fix · exloong/superset` | `webhook:incoming` | `start_session` with the coding-agent instructions |

Relay's deterministic controller stays authoritative: the GitHub webhook is
still HMAC-verified and gated in Relay, and the fix automation is only reached
after the owner's `confirm_bug` decision. When a gate opens, the worker POSTs
the task envelope (task id, issue key, immutable commit, budget, quoted
reporter context) to the automation's inbox with the `X-Webhook-Secret`
header; Devin's automation starts the session. The worker then lists sessions
with `?automation_ids=<id>`, links each new session to its dispatch by the
echoed `task_id` (or, failing that, oldest dispatch → oldest session) and polls
it exactly as before, so the dashboard, session panels, and URLs are unchanged.

The inbox secret is returned by Devin once, at creation. Relay stores it only
in the `devin_automations` table (never in the API, logs, or browser); if the
row is lost, the worker retires the automation and creates a fresh one. Relay
does not use the native `github:issues` trigger because the
context-completeness gate and the owner authorization cannot be expressed as
trigger conditions and it would bypass Relay's signature and repository checks.

### Managing Devin Automations from the UI

The **Devin Automations** view lists every automation in the Devin
organization, not only the two Relay uses. For each one it shows the workflow
(trigger event types → `start_session` action with its prompt), metadata,
enabled state, timestamps, last invocation, and the Devin sessions the
automation has launched (Devin's list filtered by `automation_ids`, merged
with Relay's own session records where Relay dispatched the task). Operators
can create, edit, enable/disable, and delete automations; Relay-managed ones
are labelled with their role (reproduction: auto after triage, fix: owner
authorized only).

The API is a thin authenticated proxy over the Automations API so the Devin
token and inbox secrets stay on the server:

| Endpoint | Role | Devin call |
| --- | --- | --- |
| `GET /api/v1/automations` | reader | `GET /organizations/{org}/automations` (all pages) |
| `GET /api/v1/automations/{id}` | reader | `GET …/automations/{id}` + `GET …/sessions?automation_ids={id}` |
| `GET /api/v1/automations/{id}/sessions` | reader | `GET …/sessions?automation_ids={id}` |
| `POST /api/v1/automations` | operator | `POST …/automations` (`webhook:incoming` → `start_session`) |
| `PATCH /api/v1/automations/{id}` | operator | `PATCH …/automations/{id}` |
| `DELETE /api/v1/automations/{id}` | operator | `DELETE …/automations/{id}` |

Responses omit the inbox URL and secret. Devin `401/403/404` answers surface as
`403/403/404`; other transport failures as `502`. The `relay_kind` and
`relay_repo` metadata keys are reserved for the worker and rejected on create.

### Health & throughput dashboard

The **Health dashboard** view (`GET /api/v1/dashboard`) exists to prove whether
the pipeline is working. Every number is aggregated from persisted records:

- Throughput: issues entered vs. completed over the period, per-day buckets,
  and the current in-flight count per stage (conversion funnel).
- Devin automation health, separately for reproduction and fix sessions:
  queued / running / succeeded / failed counts, success rate, median duration,
  and a recent-sessions table linking to each session.
- Liveness: last GitHub webhook received, last Devin session launched, worker
  heartbeat, database probe, and GitHub / Devin provider mode
  (`connected`, `dry_run`, `stale`, `unconfigured`). These derive a single
  `healthy` / `degraded` / `down` status with reasons.
- Needs attention: issues currently waiting on an owner or blocked.

In demo mode the dashboard renders clearly-labelled sample data with the
heartbeat marked *down*; it never pretends a provider is connected.

### Services

| Service | Responsibility |
| --- | --- |
| `web` | Builds and serves the React dashboard; Nginx proxies `/api/v1` to the API on the same origin. |
| `api` | Provides FastAPI query/command endpoints, authentication, database migrations, health/readiness, signed webhook ingress, and the authenticated proxy for managing Devin Automations. |
| `worker` | Claims durable jobs, advances timers and transitions, provisions Relay's two Devin Automations, dispatches gated tasks to their inboxes, and calls external providers through bounded HTTPS clients. |
| `postgres` | Persists lifecycle state, idempotency records, jobs, evidence, sessions, PR bindings, reviews, approvals, and worker status. |

The API and worker share the backend package but run as separate processes.
PostgreSQL is both the system of record and the first implementation's durable
job queue, avoiding a second state system.

### Demo and live modes

| Mode | Purpose | External effects |
| --- | --- | --- |
| `demo` | Local evaluation and UI exploration with seeded scenarios. | None. GitHub, Devin, Review, and CODEOWNERS behavior is deterministic and fake. |
| `live` | Operate the real workflow for `exloong/superset`. | Sends authenticated requests to GitHub and Devin after policy and human gates allow them. |

Live mode is the default and fails closed if required configuration is absent.
It never silently substitutes demo records or fake provider results.

## Run locally

### Prerequisites

For the recommended full-stack setup:

- Docker Engine with Docker Compose v2;
- `curl` for the health checks below.

For source-level development, also install:

- Node.js 20 or newer and npm;
- Python 3.10 or newer;
- [`uv`](https://docs.astral.sh/uv/).

### Quick start: credential-free demo

The demo is the safest and fastest way to run the complete application locally.
It builds all services, creates the database, applies migrations, seeds example
records, and starts a worker without calling GitHub or Devin.

```bash
git clone https://github.com/exloong/Devin-DE-Solution.git
cd Devin-DE-Solution

docker compose \
  -f docker-compose.yml \
  -f docker-compose.demo.yml \
  up --build --wait
```

Verify the stack:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.demo.yml \
  ps

curl --fail http://127.0.0.1:4173/api/v1/health
curl --fail http://127.0.0.1:4173/api/v1/ready
```

Open `http://127.0.0.1:4173`. Demo mode authenticates commands with a local demo
principal, so no operator or provider token is required. (Live mode does: see
[Operator token](#operator-token-required-to-sign-in).)

To use another host port:

```bash
APP_PORT=8080 docker compose \
  -f docker-compose.yml \
  -f docker-compose.demo.yml \
  up --build --wait
```

Then open `http://127.0.0.1:8080`.

Useful lifecycle commands:

```bash
# Follow API and worker activity.
docker compose \
  -f docker-compose.yml \
  -f docker-compose.demo.yml \
  logs -f api worker

# Stop containers but retain PostgreSQL data.
docker compose \
  -f docker-compose.yml \
  -f docker-compose.demo.yml \
  down

# Stop containers and delete all local Relay state.
docker compose \
  -f docker-compose.yml \
  -f docker-compose.demo.yml \
  down --volumes
```

### Develop components from source

Use Docker Compose when you need the integrated browser, API, worker, and
PostgreSQL flow. The commands below are useful for isolated frontend or backend
development.

Install frontend dependencies and start Vite:

```bash
npm ci
npm run dev
```

Vite serves the frontend at `http://127.0.0.1:4173`. The production frontend
expects `/api/v1` on the same origin; the Docker `web` service provides that
proxy. A standalone Vite process without a proxying API will show the
fail-closed API-unavailable state rather than silently loading demo data.
To point the dev server at a local API instead, set
`RELAY_API_PROXY=http://127.0.0.1:8000` (in the environment or `.env.local`)
before `npm run dev`.

Install backend dependencies:

```bash
cd backend
uv sync --extra dev
```

Run an isolated demo API with SQLite:

```bash
RELAY_MODE=demo RELAY_SEED=1 \
  uv run uvicorn app.api.app:default_app --factory --reload
```

The API is available at `http://127.0.0.1:8000`; interactive OpenAPI
documentation is at `http://127.0.0.1:8000/api/v1/docs`. In another terminal,
from the same `backend` directory, start the demo worker against the same
SQLite database:

```bash
RELAY_MODE=demo uv run python -m app.runtime.worker
```

Delete `backend/relay.sqlite3` when both processes are stopped to reset this
isolated backend environment.

### Validate changes

Frontend:

```bash
npm run typecheck
npm run build
npm run test:fixtures
```

Backend:

```bash
cd backend
uv run pytest
uv run ruff check app tests
uv run mypy app
```

Integrated smoke test:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.demo.yml \
  up --build --wait

curl --fail http://127.0.0.1:4173/api/v1/ready
```

## Configure live mode

Do not use live mode until GitHub and Devin credentials are scoped for this
deployment. Copy the example, replace every placeholder, and restrict access to
the resulting file:

```bash
cp .env.example .env
chmod 600 .env
```

| Variable | Service | Purpose |
| --- | --- | --- |
| `RELAY_MODE` | API, worker | `live` for real providers or `demo` for deterministic local adapters. |
| `RELAY_SEED` | API | Set to `0` for an empty live database; demo overlay sets it to `1`. |
| `RELAY_AUTH_TOKENS` | API | Semicolon-separated `token:login:role` entries. Use a random URL-safe token of at least 8 characters and the `operator` role for dashboard access. |
| `GITHUB_WEBHOOK_SECRET` | API | High-entropy HMAC secret shared only with the GitHub webhook. |
| `GITHUB_TOKEN` | Worker | Dedicated GitHub App installation token or fine-grained token scoped only to `exloong/superset`. |
| `GITHUB_DEFAULT_BRANCH` | Worker | Superset branch resolved to the immutable reproduction base; defaults to `master`. |
| `RELAY_TRUSTED_LABEL` | API | Optional controller-gate label. When set, `issues.opened`/`issues.labeled` events enroll an issue only if it carries (or is being given) this label. |
| `RELAY_TRUSTED_ACTORS` | API | Optional comma-separated GitHub logins; when set, only these senders can trigger enrollment. |
| `DEVIN_API_TOKEN` | API, Worker | Devin service-user API key. The worker dispatches to Devin Automations; the API proxies automation management. |
| `DEVIN_ORG_ID` | API, Worker | Devin organization identifier used by the v3 API. |
| `DEVIN_REVIEW_TOKEN` | Worker | Optional separate Devin Review token; defaults to `DEVIN_API_TOKEN`. |
| `APP_PORT` | Web | Loopback-only HTTP port; defaults to `4173`. |

Every secret except `DEVIN_ORG_ID` also supports a mutually exclusive `_FILE`
variant, for example `GITHUB_TOKEN_FILE=/run/secrets/github_token`. Secret
values are read at runtime; they are not returned by the API, logged by the
HTTPS transport, persisted in lifecycle records, or embedded in the frontend.

Start a clean live stack:

```bash
docker compose up --build --wait
curl --fail http://127.0.0.1:4173/api/v1/health
curl --fail http://127.0.0.1:4173/api/v1/ready
```

### Operator token (required to sign in)

In live mode every dashboard view (health, sessions, Devin Automations) is
behind a Relay-issued operator token. There is nothing to obtain from GitHub or
Devin: you generate the token yourself and register it with the API.

```bash
# 1. Generate a random token.
TOKEN=$(openssl rand -hex 16)

# 2. Register it in .env as token:login:role (the role must be `operator`).
echo "RELAY_AUTH_TOKENS=${TOKEN}:relay-operator:operator" >> .env
```

Open `http://127.0.0.1:4173`, go to **Connections → Dashboard operator
access**, paste the token, and click **Connect**. Until then the dashboard
shows "commands require an authenticated principal". The browser keeps the
token in `sessionStorage` for that tab only (every operator and every new tab
enters it again); provider credentials remain server-side.

### GitHub requirements

Use a dedicated GitHub App installed only on `exloong/superset`, or a
fine-grained token restricted to that repository. Relay needs:

- repository metadata: read;
- issues: read and write;
- contents: read and write;
- pull requests: read and write.

Relay has no merge or issue-close command in its typed GitHub boundary. Still
protect the default branch with required human review and do not grant the App
an administrative bypass.

Configure a GitHub webhook with:

- payload URL: `https://<relay-host>/api/v1/webhooks/github`;
- content type: `application/json`;
- secret: the exact `GITHUB_WEBHOOK_SECRET` value;
- events: Issues, Issue comments, Pull requests, and Pull request reviews;
- SSL verification enabled.

Relay verifies HMAC-SHA256 over the raw request body, rejects bodies larger than
1 MiB, validates and deduplicates deliveries, and rejects repositories other
than `exloong/superset`. GitHub `ping` events are accepted for webhook setup
verification without creating lifecycle work.

### Devin and Devin Review requirements

Create a service user under **Devin Settings → Service users**, assign the
minimum role that can create, read, message, and cancel organization sessions
and manage and view organization automations and sessions
(`ManageOrgAutomations`, `ViewOrgAutomations`, `ViewOrgSessions`), then
generate its API key. Set the key as `DEVIN_API_TOKEN` and set
`DEVIN_ORG_ID` to the corresponding `org-...` identifier. See Devin's
[authentication documentation](https://docs.devin.ai/api-reference/authentication).

Relay session requests:

- allow only `repos: ["exloong/superset"]`;
- set `resumable: false`;
- set `bypass_approval: false`;
- send no additional `secret_ids`;
- use explicit wall-clock and capability budgets;
- request typed structured results;
- quote reporter context as untrusted, inert data.

Devin Review runs against the exact Superset PR and requested head SHA. Use
`DEVIN_REVIEW_TOKEN` when Review has a separate credential, or omit it to reuse
the Devin service-user key. See
[Trigger Devin Review](https://docs.devin.ai/api-reference/v3/pr-reviews/post-enterprise-pr-reviews).

### Docker secret files

For Docker-managed secret files, set the host paths and add the secrets overlay:

```bash
export RELAY_AUTH_TOKENS_FILE_HOST=/secure/relay_auth_tokens
export GITHUB_WEBHOOK_SECRET_FILE_HOST=/secure/github_webhook_secret
export GITHUB_TOKEN_FILE_HOST=/secure/github_token
export DEVIN_API_TOKEN_FILE_HOST=/secure/devin_api_token

docker compose \
  -f docker-compose.yml \
  -f docker-compose.secrets.yml \
  up --build --wait
```

The `relay_auth_tokens` file uses
`token:login:role[;token:login:role...]`. Do not commit `.env`, secret files,
API keys, webhook secrets, or credentials.

### HTTPS

Point a public DNS name at the host, allow inbound TCP 80/443 and UDP 443, then
run the Caddy overlay:

```bash
export RELAY_HOST=relay.example.com
docker compose \
  -f docker-compose.yml \
  -f docker-compose.https.yml \
  up --build --wait
```

Caddy obtains and renews the certificate, redirects HTTP to HTTPS, and proxies
the dashboard and API over the internal Docker network. The base web port
remains bound to host loopback only. In cloud or Kubernetes deployments,
terminate TLS at the load balancer or Ingress, forward to `web:80`, and
preserve the raw webhook request body.

## Troubleshooting

- **A live worker exits immediately:** inspect `docker compose logs worker`.
  Live mode names the first missing `GITHUB_TOKEN`, `DEVIN_API_TOKEN`, or
  `DEVIN_ORG_ID`.
- **`ready` reports `unavailable`:** the worker has not written a heartbeat;
  check its mode, credentials, organization ID, and logs.
- **The dashboard returns 401:** enter a token that exactly matches an entry in
  `RELAY_AUTH_TOKENS`. Provider credentials cannot authenticate the dashboard.
- **A webhook returns 401:** verify GitHub and Relay use the same secret and
  that no proxy rewrites the raw request body.
- **A webhook returns 403:** Relay received an event for a repository other
  than `exloong/superset`.
- **A webhook returns 409:** the delivery ID was already processed.
- **A Devin request fails:** confirm the service user can use organization
  sessions and that `DEVIN_ORG_ID` has the `org-...` form.
- **The Devin Automations view shows 403 or "not configured":** the `api`
  service needs `DEVIN_API_TOKEN`/`DEVIN_ORG_ID` too, and the service user
  needs `ViewOrgAutomations` (read), `ManageOrgAutomations` (create, edit,
  delete) and `ViewOrgSessions` (sessions under an automation).
- **A dispatched task never gets a session:** check the automation is enabled
  in Devin and its last invocation status in the Automations view; a `403`
  from the inbox means the stored secret no longer matches, so delete the
  automation and restart the worker to re-create it.
- **Review remains pending:** confirm Devin Review is enabled and its token can
  call the enterprise PR-review endpoint.
- **Port 4173 is already in use:** set `APP_PORT` to another host port.

## Further reading

- [Issue automation platform architecture](docs/architecture/issue-automation-platform.md)
- [Parallel implementation and approval plan](docs/plans/parallel-implementation-plan.md)
- [Superset issue-intake research](public/reports/apache-superset/issue-intake-2025-09-05-to-2026-09-04.html)
