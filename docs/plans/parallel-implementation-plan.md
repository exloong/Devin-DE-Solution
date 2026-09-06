# Parallel implementation plan

## Branch and approval model

```text
main
  └─ feature/superset-issue-automation
       ├─ agent/backend-core
       ├─ agent/integration-adapters
       ├─ agent/frontend-live-data
       └─ agent/runtime-integration        (starts after the first three merge)
```

Each agent works in an isolated Devin workspace and opens a pull request into
`feature/superset-issue-automation`. No child branch targets `main`. The
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
- mock GitHub, Devin session, and Devin Review adapters by default;
- live adapters restricted to `exloong/superset`;
- `/api/v1` for frontend/backend communication;
- UUID resource IDs, UTC timestamps, and explicit resource versions;
- server-derived authenticated principals for every command;
- `Idempotency-Key` and `If-Match` command headers;
- deterministic lifecycle transitions and human gates.

Agents may implement live adapter clients, but tests must not require network
access or credentials. Live configuration must restrict issue intake,
reproduction checkouts, branches, pull requests, and review requests to
`exloong/superset`. No adapter may execute reporter content, merge a pull
request, close an issue as fixed, or publish a suspected security report.

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
- callers cannot assign themselves owner, security, or operator roles;
- approval and review evidence are bound to the current pull-request head;
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
4. A GitHub client boundary restricted to `exloong/superset`, including
   reviewer requests and `.github/CODEOWNERS`-based candidate routing.
5. Typed Devin session task/result envelopes and a v3 client boundary for
   create, inspect, list, message, outputs, canonical session links, and
   bounded cancellation through the documented session archive endpoint.
6. A Devin Review client boundary for trigger, status, and findings.
7. Deterministic mock adapters for classification, reproduction, evidence,
   fix, session progress, conversation, and review results.
8. Capability, budget, issue-revision, target-commit, and late-result checks.
9. Tests for malformed signatures, unauthorized repositories, duplicate
   deliveries, stale results, owner ambiguity, and prohibited capabilities.

**Acceptance criteria**

- no network call is required by tests;
- all repository operations reject targets other than `exloong/superset`;
- reviewer routing covers every changed path and fails closed on ambiguity;
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
7. A Devin-like session detail with synchronized conversation, progress,
   outputs, PR/review state, and authenticated session/desktop links.
8. Clear disclosure when conversation or remote-computer data is available
   only through an external Devin link.
9. Focused component tests if the workstream adds a test runner; otherwise
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
4. GitHub webhook processing restricted to `exloong/superset`.
5. Seeded scenarios matching the mockup.
6. One end-to-end dry run:
   `new → triage → awaiting_reporter → reproducing →
   needs_owner_decision → fix_authorized`.
7. One adapter contract run that proves a confirmed flow would create a
   Superset-targeted fix PR, trigger Devin Review, and request owners without
   performing those network writes.
8. Explicit proof that no workspace remains during reporter/owner waits.
9. Health checks and repeatable local startup.

**Acceptance criteria**

- `docker compose up --build` reaches healthy status;
- duplicate dry-run events remain idempotent;
- restarting the worker preserves jobs and lifecycle state;
- browser data comes from the API when healthy and shows demo mode otherwise;
- no real GitHub or Devin credentials are needed;
- every generated repository reference is `exloong/superset`;
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
7. inspect synchronized conversation, progress, outputs, PR/review state, and
   the authenticated Devin session/desktop links;
8. confirm that the workspace is released before an owner wait;
9. approve or reject the expected-behavior decision;
10. confirm that a fix PR targets `exloong/superset`, Devin Review is requested,
    and the correct owner-routing rationale is displayed;
11. confirm that owner approval authorizes work but does not merge it;
12. restart the stack and observe preserved lifecycle state.

## Review ownership

The parent session is the integration owner. It will:

- inspect every child PR and its validation evidence;
- reject unrelated or unsafe changes;
- merge approved child work only into the feature branch;
- run cumulative validation after each merge;
- keep the final feature PR unmerged until the user verifies and approves it.
