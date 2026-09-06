# Relay issue operations control plane

Relay is a standalone control plane for a Devin-powered issue intake,
reproduction, and fix workflow. It accepts signed GitHub events only from
`exloong/superset`, runs bounded Devin sessions against an immutable Superset
commit, and keeps every consequential decision behind a human gate.

Relay never automatically merges a pull request or closes an issue. Devin
Review is advisory and does not count as owner approval. Private Devin session
and desktop links are available only to authenticated dashboard operators and
are rejected from public GitHub issue comments.

## Architecture

The Docker stack contains four long-running services:

| Service | Responsibility |
| --- | --- |
| `web` | React dashboard plus an Nginx same-origin proxy for `/api/v1`. |
| `api` | FastAPI lifecycle API, signed GitHub webhook ingress, authentication, migrations, and human commands. |
| `worker` | Durable job consumer that calls GitHub, Devin Sessions, and Devin Review through bounded HTTPS clients. |
| `postgres` | Persistent issues, revisions, jobs, sessions, evidence, conversations, PR bindings, reviews, and approvals. |

The lifecycle is:

```text
new → triage → awaiting_reporter → reproducing → needs_owner_decision
    → fix_authorized → fixing → pr_open → awaiting_owner
```

Each issue revision, session, review, and approval is bound to its repository,
issue revision, target commit, PR number, and PR head SHA. A synchronized PR
invalidates review and owner-approval evidence for the previous head.

Detailed documents:

- [Issue automation platform architecture](docs/architecture/issue-automation-platform.md)
- [Parallel implementation and approval plan](docs/plans/parallel-implementation-plan.md)
- [Superset issue-intake research](public/reports/apache-superset/issue-intake-2025-09-05-to-2026-09-04.html)

## Production configuration

Production is the default: `RELAY_MODE=live` and `RELAY_SEED=0`. Relay starts
with an empty database and never falls back to browser demo fixtures. Copy the
example, replace every placeholder, and restrict access to the resulting file:

```bash
cp .env.example .env
chmod 600 .env
```

| Variable | Service | Purpose |
| --- | --- | --- |
| `RELAY_AUTH_TOKENS` | API | Semicolon-separated `token:login:role` entries. Use a random URL-safe token of at least 8 characters and the `operator` role for dashboard access. |
| `GITHUB_WEBHOOK_SECRET` | API | High-entropy HMAC secret shared only with the GitHub webhook. |
| `GITHUB_TOKEN` | Worker | Dedicated GitHub App installation token or fine-grained token scoped only to `exloong/superset`. |
| `GITHUB_DEFAULT_BRANCH` | Worker | Superset branch resolved to the immutable reproduction base; defaults to `master`. |
| `DEVIN_API_TOKEN` | Worker | Devin service-user API key or personal access token. |
| `DEVIN_ORG_ID` | Worker | Devin organization identifier used by the v3 session API. |
| `DEVIN_REVIEW_TOKEN` | Worker | Optional separate Devin Review token; defaults to `DEVIN_API_TOKEN`. |
| `APP_PORT` | Web | Loopback-only HTTP port, default `4173`. |

Every secret except `DEVIN_ORG_ID` also supports a mutually exclusive `_FILE`
variant, for example `GITHUB_TOKEN_FILE=/run/secrets/github_token`. Secret
values are read at runtime; they are not persisted in lifecycle records,
returned by the API, logged by the HTTPS transport, or embedded in the frontend
bundle.

For Docker-managed secret files, set these host paths and add the secrets
overlay:

```bash
export RELAY_AUTH_TOKENS_FILE_HOST=/secure/relay_auth_tokens
export GITHUB_WEBHOOK_SECRET_FILE_HOST=/secure/github_webhook_secret
export GITHUB_TOKEN_FILE_HOST=/secure/github_token
export DEVIN_API_TOKEN_FILE_HOST=/secure/devin_api_token

docker compose -f docker-compose.yml -f docker-compose.secrets.yml up --build -d
```

The `relay_auth_tokens` file contains the same
`token:login:role[;token:login:role...]` format as the environment variable.
Do not commit `.env`, secret files, API keys, webhook secrets, or credentials.

## Provision GitHub

Create a dedicated GitHub App installed only on `exloong/superset`, or a
fine-grained token restricted to that repository. Relay needs:

- repository metadata: read;
- issues: read and write, for intake and reporter comments;
- contents: read and write, for immutable commit reads and fix branches;
- pull requests: read and write, for PR creation, synchronization, review
  state, and reviewer requests.

Relay has no merge or issue-close command in its typed GitHub boundary. Still
protect the default branch with required human review and do not grant the App
an administrative bypass.

Create a GitHub webhook with:

- payload URL: `https://<relay-host>/api/v1/webhooks/github`;
- content type: `application/json`;
- secret: the exact `GITHUB_WEBHOOK_SECRET` value;
- events: Issues, Issue comments, Pull requests, and Pull request reviews;
- SSL verification enabled.

Relay verifies HMAC-SHA256 over the raw request body, rejects bodies larger than
1 MiB, validates the delivery ID and event envelope, deduplicates deliveries,
and rejects repositories other than `exloong/superset`.

## Provision Devin and Devin Review

Create a service user under **Devin Settings → Service users**, assign the
minimum role that can create, read, message, and cancel organization sessions,
then generate its API key. Set the key as `DEVIN_API_TOKEN` and copy the
organization ID from the same settings area into `DEVIN_ORG_ID`. Devin's API
documentation is at
[Authentication](https://docs.devin.ai/api-reference/authentication).

The GitHub identity available to that Devin organization must have access only
to the intended target repository for this deployment. Relay session requests:

- allow only `repos: ["exloong/superset"]`;
- set `resumable: false`;
- set `bypass_approval: false`;
- send no additional `secret_ids`;
- use explicit wall-clock and capability budgets;
- request typed structured results;
- quote reporter context as untrusted, inert data.

Devin Review uses `POST /v3/enterprise/pr-reviews` for the exact Superset PR.
The response must name the PR head SHA Relay requested; a different head fails
closed. Use `DEVIN_REVIEW_TOKEN` when Review has a separate credential, or omit
it to use the Devin service-user key. See
[Trigger Devin Review](https://docs.devin.ai/api-reference/v3/pr-reviews/post-enterprise-pr-reviews).

## Start a clean deployment

```bash
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:4173/api/v1/health
curl --fail http://127.0.0.1:4173/api/v1/ready
```

`health` confirms the API process and repository scope. `ready` checks the
database migration and reports the worker heartbeat as `ok`, `stale`, or
`unavailable`. A live worker exits with a clear missing-configuration error
rather than substituting fake sessions, PRs, reviews, or routing.

Open `http://127.0.0.1:4173`, go to **Configuration → Connections**, and enter
the Relay operator token. The browser keeps it in `sessionStorage`; GitHub and
Devin provider secrets remain server-side.

PostgreSQL state is retained in the `relay_postgres` volume:

```bash
docker compose down                 # keep state
docker compose down --volumes       # remove all Relay state
docker compose up --build -d        # clean start: zero lifecycle records
```

## HTTPS deployment

Point a public DNS name at the host, allow inbound TCP 80/443 and UDP 443, then
run the Caddy overlay:

```bash
export RELAY_HOST=relay.example.com
docker compose -f docker-compose.yml -f docker-compose.https.yml up --build -d
```

Caddy obtains and renews the public certificate, redirects HTTP to HTTPS, and
proxies the dashboard and API over the internal Docker network. The base web
port remains bound to host loopback only. In cloud or Kubernetes deployments,
terminate TLS at the load balancer/Ingress and forward to `web:80`; preserve the
raw webhook request body.

Outbound GitHub and Devin clients accept HTTPS URLs only, use bounded timeouts
and retries, propagate correlation IDs, and map network failures to typed
errors without logging authorization tokens.

## Verify signed webhook handling

With the stack running, create a minimal delivery and sign its exact bytes:

```bash
payload='{"action":"opened","repository":{"full_name":"exloong/superset"},"issue":{"number":123,"title":"Relay verification","body":"Steps to reproduce: ...","user":{"login":"relay-test"}}}'
signature="sha256=$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" -hex | sed 's/^.* //')"

curl --fail-with-body -X POST \
  http://127.0.0.1:4173/api/v1/webhooks/github \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: issues' \
  -H 'X-GitHub-Delivery: relay-readme-verification-1' \
  -H "X-Hub-Signature-256: $signature" \
  --data-binary "$payload"
```

The response is `202 Accepted`. Changing either the body or signature produces
`401`, replaying the delivery ID produces `409`, and changing
`repository.full_name` produces `403`.

Use a dedicated, clearly labelled test issue in `exloong/superset` for the
final callback test. Leave its generated PR unmerged and the issue open until a
human has inspected the complete flow.

## Explicit demo mode

Demo mode is credential-free, seeded, and performs no network side effects:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build -d
```

It uses deterministic fake GitHub, Devin, Review, and CODEOWNERS behavior. It
must not be used to validate production credentials or callback delivery.

## Troubleshooting

- **Worker exits immediately:** inspect `docker compose logs worker`; live mode
  names the first missing `GITHUB_TOKEN`, `DEVIN_API_TOKEN`, or `DEVIN_ORG_ID`.
- **`ready` says `unavailable`:** the worker has not written a heartbeat; check
  its credentials, organization ID, and logs.
- **Dashboard returns 401:** connect a token that exactly matches an entry in
  `RELAY_AUTH_TOKENS`; provider credentials cannot authenticate the dashboard.
- **Webhook returns 401:** verify GitHub and Relay use the same secret and that
  no proxy rewrites the raw body.
- **Webhook returns 403:** Relay received an event for a repository other than
  `exloong/superset`.
- **Devin request fails:** confirm the service user can use organization
  sessions and that `DEVIN_ORG_ID` has the `org-...` form.
- **Review stays pending:** confirm Devin Review is enabled for the account and
  its token can call the enterprise PR-review endpoint.

## Local development and validation

```bash
npm install
npm run dev
```

```bash
cd backend
uv sync --extra dev
uv run uvicorn app.api.app:default_app --factory --reload
uv run python -m app.runtime.worker
```

Validation:

```bash
npm run typecheck
npm run build
npm run test:fixtures

cd backend
uv run pytest
uv run ruff check app tests
uv run mypy app

docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build --wait
curl --fail http://127.0.0.1:4173/api/v1/ready
```
