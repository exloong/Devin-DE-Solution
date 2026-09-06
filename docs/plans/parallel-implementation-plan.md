# Parallel implementation plan

## Branch and approval model

```text
main
  └─ feature/issue-automation-platform
       ├─ agent/backend-core
       ├─ agent/integration-adapters
       ├─ agent/frontend-live-data
       └─ agent/runtime-integration        (starts after the first three merge)
```

Each agent works in an isolated Devin workspace and opens a pull request into
`feature/issue-automation-platform`. No child branch targets `main`. The
integration owner reviews, validates, and merges child work. The feature branch
is exposed for manual verification before its final PR may be approved and
merged into `main`.

## Shared contracts

All workstreams follow
`docs/architecture/issue-automation-platform.md`.

The initial implementation uses:

- React/Vite for the existing web app;
- a typed Python ASGI backend;
- PostgreSQL as the source of truth and durable job queue;
- mock GitHub and agent adapters by default;
- `/api/v1` for frontend/backend communication;
- UUID resource IDs, UTC timestamps, and explicit resource versions;
- deterministic lifecycle transitions and human gates.

Agents must not connect to Apache Superset, use production credentials, create
real Devin sessions, execute reporter content, merge pull requests, or close
issues.

## Wave 1: independent workstreams

### A. Backend lifecycle core

**Owned paths**

- `backend/app/domain/`
- `backend/app/persistence/`
- `backend/app/api/`
- `backend/tests/domain/`
- `backend/tests/api/`
- `backend/pyproject.toml`

**Deliverables**

1. Typed lifecycle states, events, transition results, and authorization
   decisions.
2. A deterministic transition engine with explicit legal source states.
3. Persistence models and repositories for issues, revisions, events,
   transition attempts, sessions, jobs, and human decisions.
4. Initial schema migration.
5. Query APIs for issues, sessions, workflow, and analytics summary.
6. Command APIs for local dry runs, reporter responses, and owner decisions.
7. Health and readiness endpoints.
8. Unit and API tests using mock adapters only.

**Acceptance criteria**

- duplicate events do not create duplicate transitions;
- illegal transitions return a stable conflict code;
- unavailable reporter evidence does not become reproduction-ready;
- owner confirmation is required before `fix_authorized`;
- waiting states do not report a running workspace;
- tests, formatting, lint, and type checking pass.

### B. GitHub and agent integration contracts

**Owned paths**

- `backend/app/integrations/`
- `backend/tests/integrations/`

**Deliverables**

1. GitHub webhook envelope, signature verification, allowlist, and delivery
   deduplication helpers.
2. Side-effect command types for comments, labels, branches, and pull requests.
3. A fake GitHub adapter that records commands without network writes.
4. Typed agent task/result envelopes and output validation.
5. A deterministic mock agent adapter for classification, reproduction,
   evidence-packet, and fix tasks.
6. Capability, budget, issue-revision, and late-result checks.
7. Tests for malformed signatures, unauthorized repositories, duplicate
   deliveries, stale results, and prohibited capabilities.

**Acceptance criteria**

- no network call is required by tests;
- public writes and agent capabilities are explicit typed commands;
- merge, issue closure, security publication, and reporter script execution
  have no supported command;
- untrusted text is never passed to a shell;
- integration tests pass independently of the web app.

### C. Frontend live-data boundary

**Owned paths**

- `src/api/`
- `src/hooks/`
- `src/components/`
- focused changes in `src/App.tsx`, `src/data.ts`, and `src/styles.css`

**Deliverables**

1. Typed `/api/v1` client and resource schemas.
2. Data hooks for issue lists/details, session lists/details, workflow, and
   analytics.
3. Clear live, demo-data, loading, empty, stale, and error indicators.
4. Reporter-response and owner-decision commands with pending/error states.
5. Session filters driven by API data rather than hardcoded counts.
6. A local demo-data fallback that never mixes with live records.
7. Focused component tests if the workstream adds a test runner; otherwise
   typecheck and production build validation.

**Acceptance criteria**

- the existing mockup remains usable when the API is absent;
- live and demo records are visually distinguishable;
- failed commands do not optimistically claim a lifecycle transition;
- required answers and safe-alternative routing retain current behavior;
- typecheck and production build pass.

## Wave 1 merge order

The three workstreams are independent and may run concurrently. Merge order:

1. backend lifecycle core;
2. integration contracts, resolving package-level imports only;
3. frontend live-data boundary.

The integration owner runs each workstream's scoped tests before merge. Any
behavioral contract disagreement is resolved in the architecture document
before code is adapted.

## Wave 2: runtime integration

This work starts only after all Wave 1 work is reviewed and merged.

**Owned paths**

- backend application composition and worker entry points;
- `docker-compose.yml`;
- backend/web Dockerfiles;
- `nginx.conf`;
- integration test configuration;
- local seed and dry-run scripts;
- README setup and verification instructions.

**Deliverables**

1. Compose services for web, API, worker, and PostgreSQL.
2. Nginx proxying for `/api/`.
3. Worker job claiming, transition execution, retry, and outbox delivery.
4. Seeded scenarios matching the mockup.
5. One end-to-end dry run:
   `new → triage → awaiting_reporter → reproducing →
   needs_owner_decision → fix_authorized`.
6. Explicit proof that no workspace remains during reporter/owner waits.
7. Health checks and repeatable local startup.

**Acceptance criteria**

- `docker compose up --build` reaches healthy status;
- duplicate dry-run events remain idempotent;
- restarting the worker preserves jobs and lifecycle state;
- browser data comes from the API when healthy and shows demo mode otherwise;
- no real GitHub or Devin credentials are needed;
- integration tests, frontend typecheck, and production build pass.

## Wave 3: hardening and human verification

1. Review the cumulative feature diff against `main`.
2. Run dependency, secret, and unsafe-command scans.
3. Verify API redaction, webhook rejection, and human authorization paths.
4. Run the full automated validation suite.
5. Publish the feature-branch preview and a manual verification checklist.
6. Fix findings on the feature branch.
7. Open a feature PR to `main`.
8. Wait for explicit human approval; do not auto-merge.

## Manual verification checklist

The final preview must let a reviewer:

1. create a synthetic issue dry run;
2. see the deterministic intake and triage timeline;
3. answer a required reporter question;
4. choose `I cannot provide this` and observe safe-alternative routing;
5. advance a complete report to a bounded mock reproduction session;
6. inspect session trigger, budget, events, artifacts, and guardrails;
7. confirm that the workspace is released before an owner wait;
8. approve or reject the expected-behavior decision;
9. confirm that owner approval authorizes work but does not merge it;
10. restart the stack and observe preserved lifecycle state.

## Review ownership

The parent session is the integration owner. It will:

- inspect every child PR and its validation evidence;
- reject unrelated or unsafe changes;
- merge approved child work only into the feature branch;
- run cumulative validation after each merge;
- keep the final feature PR unmerged until the user verifies and approves it.
