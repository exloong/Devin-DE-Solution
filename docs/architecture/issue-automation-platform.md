# Relay issue automation platform

## Status

Proposed architecture for the first implementation behind the Relay mockup.
The mockup remains the product contract; this document defines the services,
state, controls, and interfaces required to make the demonstrated flows real.

`exloong/Devin-DE-Solution` contains and runs the standalone control plane,
dashboard, API, worker, and persistence services. `exloong/superset` is the
single configured target repository: a new issue there triggers the flow,
bounded Devin sessions reproduce and fix against its code, and approved fixes
open pull requests back to that repository.

The first implementation defaults to dry-run adapters. Live mode may receive
signed issue events, create bounded Devin sessions restricted to
`exloong/superset`, open a fix pull request after a human confirms the bug,
trigger Devin Review, and request reviewers. It must never execute
reporter-provided scripts, merge pull requests, close confirmed bugs, or
publish security reports.

## Goals

1. Turn issue and comment events into a durable, inspectable lifecycle.
2. Keep deterministic policy separate from agent judgment.
3. Ask reporters only for missing information and preserve all prior answers.
4. Run reproduction and coding work in bounded, isolated sessions.
5. Produce portable evidence before asking an owner for a decision.
6. Require explicit human authorization before code work and explicit owner
   approval before merge.
7. Make every transition, retry, reminder, and agent run observable in the UI.
8. Recover safely from duplicate delivery, restarts, timeouts, and partial
   integration failures.
9. Give operators one place to inspect each Devin session's conversation,
   progress, outputs, pull requests, and authenticated Devin session/desktop.

## Non-goals for the first implementation

- Processing repositories other than the configured `exloong/superset` fork.
- Running reporter-supplied commands or downloading untrusted attachments.
- Automatically deciding security sensitivity after a positive signal.
- Automatically merging pull requests or closing reproduced bugs.
- Maintaining an agent workspace while waiting for a reporter or owner.
- Treating text matches as authoritative reproduction or fix milestones.
- Replacing maintainers for product, architecture, support, or release policy.

## System context

```text
exloong/superset issue/comment/review events
        │ signed GitHub webhook
        ▼
┌────────────────────┐
│ Relay API           │
│ event validation   │
│ query/command API  │
└─────────┬──────────┘
          │ transaction
          ▼
┌────────────────────┐       claims jobs       ┌────────────────────┐
│ PostgreSQL         │◀────────────────────────│ Relay worker       │
│ lifecycle + outbox │                         │ state controller   │
│ jobs + audit log   │────────────────────────▶│ agent orchestration│
└─────────┬──────────┘       writes outcomes   └─────────┬──────────┘
          │                                               │ Devin v3 API
          ▼                                               ▼
┌────────────────────┐                         ┌────────────────────┐
│ React web app      │                         │ Agent gateway      │
│ flow dashboard     │                         │ session + review   │
│ Devin session UX   │                         │ mock fallback      │
└────────────────────┘                         └────────────────────┘
          │                                               │ isolated checkout
          │                                               ▼
          └─────────────────────────────────── exloong/superset
```

The API and worker share one backend package but run as separate processes.
The worker claims durable jobs from PostgreSQL. A database-backed queue and
transactional outbox are sufficient for the first implementation and avoid
introducing a second state system. A dedicated queue can be added after load
testing demonstrates the need.

## Deployment units

| Unit | Responsibility | Local container |
|---|---|---|
| `web` | React operator, reporter, owner, and session views | nginx |
| `api` | REST API, signed webhook ingress, health/readiness | Python ASGI |
| `worker` | Timers, transitions, bounded orchestration, outbox delivery | Python process |
| `postgres` | Source of truth, job claims, idempotency, audit history | PostgreSQL |

Nginx serves the frontend and proxies `/api/` to the API. Docker Compose is the
reference local environment. Production deployment details remain outside the
first implementation, but service boundaries must not depend on Compose.

## Repository boundary

The control plane never becomes part of the Superset runtime. It integrates
through repository and Devin APIs:

1. GitHub sends issue, comment, pull-request, review, and check events from
   `exloong/superset` to Relay.
2. Relay stores the event and advances deterministic lifecycle state.
3. Relay creates a bounded Devin session with repository access restricted to
   `exloong/superset`, the issue revision, allowed capabilities, and a budget.
4. Devin works in its own remote workspace and returns session status, output,
   artifacts, and pull-request links.
5. After human bug confirmation, a new bounded fix session creates a branch
   and pull request against `exloong/superset`.
6. Relay triggers Devin Review for that pull request and routes the resulting
   evidence to the selected human owners.

The Superset fork does not need to import Relay code. Its only repository-side
configuration is the GitHub App installation, optional workflow checks, issue
templates, and ownership metadata used by Relay.

## Architectural boundaries

### Deterministic controller

The controller owns:

- webhook event filtering and authorization;
- idempotency and per-issue serialization;
- legal transition validation;
- reminder and inactivity timers;
- concurrency, retry, and runtime budgets;
- durable status and audit history;
- security fail-closed routing;
- human approval gates;
- agent-session creation and result acceptance.

The controller never delegates these responsibilities to a prompt.

### Agent gateway

Agents may:

- classify an issue with evidence and confidence;
- identify the smallest missing reproduction facts;
- create a safe reproduction plan;
- compare target and control behavior;
- draft a regression test and scoped fix after authorization;
- summarize artifacts for a reporter or owner.

Every request uses a typed task envelope:

```json
{
  "task_id": "uuid",
  "issue_id": "uuid",
  "issue_revision": 7,
  "transition": "reproduce",
  "budget_seconds": 3600,
  "allowed_capabilities": ["read_repo", "run_isolated_tests"],
  "input_artifact_ids": ["uuid"],
  "output_schema": "reproduction_result.v1"
}
```

Every result is validated against its output schema and the issue revision that
created it. Late or malformed results are retained for audit but cannot advance
the lifecycle.

The first adapter is a deterministic mock used by local development and tests.
The live adapter uses the documented Devin v3 session operations to create,
list, inspect, and message sessions, and the Devin Review endpoint to trigger a
review for a pull request. All API calls retain their request correlation ID
and raw response metadata after redaction.

Devin session prompts always name `exloong/superset`, the immutable target
commit, and the exact allowed outcome. Reproduction and fix sessions are
separate; evidence from one is passed to the next as typed artifacts rather
than by keeping a workspace alive.

### Human gates

Humans retain authority for:

- releasing a security-sensitive report to any public workflow;
- deciding expected product behavior;
- authorizing a coding session;
- approving and merging a pull request;
- closing a reproduced bug as fixed;
- changing lifecycle policy or agent capabilities.

Owner silence creates an escalation record. It never implies approval.
Human identity and role come from an authenticated server-side principal, not
from command JSON. A caller cannot claim to be an owner, security responder, or
operator. Owner decisions are authorized against the persisted reviewer
routing decision or an explicit, audited operator override.

Pull-request approval is bound to the exact repository, pull-request number,
and head commit SHA. A new head invalidates previous human approval and review
evidence, then reruns deterministic routing and Devin Review. A merge webhook
can complete a flow only when the merged head has a matching human approval.

## Lifecycle

### States

```text
new
  → triage
  → awaiting_reporter
  → reproducing
  → blocked_environment
  → needs_owner_decision
  → fix_authorized
  → fixing
  → pr_open
  → awaiting_owner
  → changes_requested
  → completed

Alternate terminal or holding states:
duplicate
not_a_bug
unsupported
closed_inactive
security_private
automation_error
```

Reopening creates a new issue revision and transition attempt; it does not
erase prior history.

### Transition contract

Each transition defines:

- accepted source states;
- triggering event type;
- authorization rule;
- deterministic preconditions;
- optional agent task and output schema;
- destination state;
- public and private side effects;
- timeout, retry, and escalation policy;
- compensating action for partial failure.

Example:

```text
awaiting_reporter + reporter_comment
  1. Validate actor and issue revision.
  2. Normalize the new response without executing content.
  3. Mark previously requested fields as answered, unavailable, or invalid.
  4. If minimum context is satisfied, enqueue classification/reproduction.
  5. Otherwise publish one focused follow-up.
  6. Cancel obsolete reminder jobs.
```

## Persistence model

All mutable records use UUID primary keys and timestamps in UTC.

| Table | Purpose |
|---|---|
| `repositories` | Allowlisted repository and dry-run policy |
| `issues` | Stable external identity and current lifecycle state |
| `issue_revisions` | Immutable normalized snapshots of issue context |
| `events` | Deduplicated inbound and internal events |
| `transition_attempts` | State-machine decision, inputs, output, and error |
| `information_requests` | Individual reporter questions and answer status |
| `agent_sessions` | Bounded task, status, budget, workspace, checkpoints |
| `artifacts` | Typed metadata for plans, logs, patches, and evidence |
| `human_decisions` | Owner/security/operator decisions with actor identity |
| `jobs` | Durable scheduled or immediately claimable work |
| `outbox_messages` | Side effects awaiting delivery |
| `audit_entries` | Append-only security and operator audit trail |

Important constraints:

- `(repository_id, external_issue_number)` is unique.
- `(source, delivery_id)` is unique for inbound events.
- only one active transition attempt exists per issue revision and transition;
- an agent result must reference the task and issue revision that created it;
- human decisions are immutable and superseded rather than edited;
- public comments contain no private artifacts, Devin session or desktop links,
  or security-sensitive content; reproduction updates state only the outcome,
  observed behavior, and verification evidence suitable for the reporter.

Per-issue work is serialized with a transaction-level advisory lock in
PostgreSQL. Job claims use `FOR UPDATE SKIP LOCKED`.

## API contract

All endpoints are under `/api/v1`.

### Query endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Database and worker readiness |
| `GET` | `/issues` | Filtered issue workbench |
| `GET` | `/issues/{id}` | Issue, revision, questions, evidence, decisions |
| `GET` | `/sessions` | Filtered agent-session monitor |
| `GET` | `/sessions/{id}` | Status, conversation, events, outputs, links |
| `GET` | `/workflow` | Active versioned lifecycle definition |
| `GET` | `/analytics/summary` | Operational metrics for the dashboard |

### Command endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhooks/github` | Signed allowlisted GitHub events |
| `POST` | `/issues/{id}/responses` | Reporter answer or unavailable marker |
| `POST` | `/issues/{id}/decisions` | Owner/security/operator decision |
| `POST` | `/issues/{id}/actions/retry` | Authorized recovery action |
| `POST` | `/sessions/{id}/actions/cancel` | Bounded cancellation request |
| `POST` | `/dry-runs` | Create a local synthetic flow run |

Commands require an `Idempotency-Key` header. Mutations of an existing issue or
session also require an `If-Match` resource-version precondition. Mutation
responses return the accepted event and current resource version; processing
may continue asynchronously. Command bodies never contain caller identity or
role.

The web app initially falls back to committed mock data when the API is
unavailable, but it must display a clear `Demo data` indicator and never mix
mock and live records in one view. A reachable API that returns an
authentication, authorization, validation, server, or schema error remains a
live error; it must never silently switch the operator to demo records.

## GitHub ingress and egress

### Ingress

1. Preserve the raw request body.
2. Validate `X-Hub-Signature-256` before parsing.
3. Reject repositories not present in the allowlist.
4. Deduplicate by GitHub delivery ID.
5. Store a redacted event record and enqueue processing in one transaction.
6. Return quickly; no agent work happens in the request.

### Egress

The fake GitHub adapter is the local default. The live adapter is restricted to
`exloong/superset` and separately authorizes each comment, label, reviewer
request, branch, and pull-request capability.

Live flow:

1. `issues.opened` creates or deduplicates an intake flow.
2. Relay asks only for missing safe context through issue comments.
3. A reproduction session checks out the recorded Superset commit.
4. A human owner decides whether the evidence confirms a bug.
5. A separately authorized fix session creates a branch and pull request in
   `exloong/superset`, linking the source issue.
6. Relay triggers Devin Review for the latest pull-request head.
7. Relay derives reviewer candidates from `.github/CODEOWNERS`, paths changed,
   prior ownership configuration, and explicit routing policy.
8. Relay requests human review; it never treats silence or Devin Review as
   merge approval.
9. A pull-request synchronize event invalidates stale approvals and review
   findings before any new agent or owner result can advance the flow.

No adapter may:

- merge a pull request;
- close a reproduced bug as fixed;
- publish a suspected security report;
- execute issue text or attachments;
- write outside the configured repository and fork policy.

## Reporter experience

Questions are stored as independent records rather than regenerated as one
prompt. Each question includes:

- the missing fact;
- why it matters;
- a safe example;
- prohibited data;
- an `I cannot provide this` route;
- answer validation;
- reminder and inactivity dates.

An unavailable answer is not treated as complete reproduction context. It
triggers a safe-alternative review that may use a public fixture, request a
different discriminator, or hand off to a maintainer.

## Owner experience

The owner packet separates:

- observed behavior;
- target and control versions;
- repeat count and environment;
- expected behavior evidence;
- minimal condition;
- regression window;
- test status;
- security classification;
- remaining uncertainty.

The decision API accepts `confirm_bug`, `request_discriminator`,
`reclassify`, and `route_security_private`. Only `confirm_bug` may create a
coding authorization, and that authorization has a scope and expiration.

Reviewer routing is deterministic and explainable. The dashboard shows every
candidate, the rule that selected them, and whether GitHub accepted the review
request. When ownership is ambiguous or no eligible owner is found, the flow
enters `needs_owner_decision` rather than guessing.

## Session lifecycle

Agent sessions use:

```text
queued → running → completed
                 ↘ failed
                 ↘ needs_attention
                 ↘ cancelled
```

`waiting_on_reporter` and `waiting_on_owner` are issue states, not running
agent states. The UI may show the completed session that produced the handoff,
but its workspace and compute allocation must already be released.

Session budgets include wall-clock timeout, retry count, allowed capabilities,
repository/branch scope, and maximum output size. Heartbeats update liveness,
not lifecycle state. Lost heartbeats lead to a bounded cancellation and
operator-visible recovery job.

### Devin-like session detail

The dashboard provides:

- title, status, elapsed time, budget, repository, commit, branch, and trigger;
- synchronized conversation messages with author, timestamp, and attachments
  when exposed by the approved Devin API;
- a progress timeline derived from session status and recorded events;
- structured outputs, pull requests, review state, and retained artifacts;
- operator actions such as send message, cancel, retry, and open the issue;
- an authenticated link to the canonical Devin session;
- an authenticated link to Devin Desktop/remote computer when the platform
  exposes one for the session.

Relay does not scrape Devin or proxy remote-desktop credentials. If conversation
or desktop embedding is not exposed by the approved API, the dashboard shows
the synchronized fields it can retrieve and opens the authenticated Devin view
for the rest. This limitation must be explicit rather than simulated as live
data.

## Security and privacy

- Secrets are supplied through runtime secret management, never persisted in
  lifecycle payloads or logs.
- Live command endpoints deny access unless an authentication provider is
  configured. Dry-run mode uses an explicit demo principal and fake adapters.
- Caller identity and authorization are derived server-side and recorded in
  immutable decision and audit records.
- Webhook signatures use constant-time comparison.
- External text is data, never shell input or prompt-level authorization.
- Artifact content types and sizes are allowlisted.
- Reporter scripts and arbitrary attachments are never executed.
- Security signals stop public processing and create a private operator task.
- Audit entries record actor, action, resource version, and correlation ID.
- API errors expose stable codes but not stack traces or private payloads.
- Logs use structured fields and redact configured keys and URL components.

## Reliability and observability

Required metrics:

- event acceptance, duplication, rejection, and processing latency;
- time in lifecycle state;
- question count and reporter response latency;
- reproduction success, unavailable evidence, and environment-block rates;
- agent queue wait, runtime, budget exhaustion, retry, and cancellation;
- owner decision latency and escalation count;
- outbox delivery attempts and dead letters.

Every event, transition, job, session, and side effect shares a correlation ID.
The operator UI links those records into one timeline.

Failures are classified as:

- invalid input;
- authorization/policy rejection;
- transient integration failure;
- environment failure;
- agent output validation failure;
- automation defect requiring operator recovery.

## Testing strategy

1. Pure state-machine unit tests cover every legal and illegal transition.
2. Property tests exercise duplicate and reordered events.
3. API tests verify auth, idempotency, optimistic versions, and redaction.
4. Worker tests verify job claims, retries, timeouts, and outbox semantics.
5. Contract tests run mock GitHub, Devin session, and Devin Review adapters.
6. Integration tests run API, worker, and PostgreSQL in Docker.
7. UI component tests cover live, demo, loading, empty, and error states.
8. Browser testing is reserved for the golden reporter and owner paths.

## Delivery constraints

- Work lands first in `feature/superset-issue-automation`.
- Parallel agents use isolated workspaces and their own branches.
- Each workstream opens a PR targeting the feature branch, never `main`.
- The integration owner reviews and merges those changes into the feature
  branch, resolves interface drift, and runs the full local stack.
- A final feature PR targets `main` but remains unmerged until human
  verification and explicit approval.

## Open decisions before production enablement

1. Which Devin v3 fields are approved for conversation synchronization and
   authenticated desktop/session linking?
2. Which GitHub App installation may receive and write to
   `exloong/superset`?
3. Which identity provider protects operator and owner commands?
4. What data-retention periods apply to issue revisions, logs, and artifacts?
5. Which security-team destination receives private routing events?
6. Which Superset setup profiles, commands, fixtures, and database connectors
   are approved for reproduction?

These decisions do not block the local mock-adapter implementation.
