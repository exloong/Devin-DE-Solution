# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.domain.models import (
    Actor,
    AgentSession,
    ConversationAuthor,
    ConversationMessage,
    Event,
    Issue,
    Job,
    JobKind,
    JobStatus,
    PullRequestState,
    SessionBudget,
    SessionEvent,
)
from app.domain.ports import UnitOfWork, UnitOfWorkFactory
from app.domain.states import ActorRole, EventType, SessionKind, SessionState
from app.domain.transitions import TransitionService
from app.integrations.codeowners import OwnerKind, ReviewerRouter, RoutingStatus
from app.integrations.devin_review import (
    DevinReviewClient,
    LiveDevinReviewClient,
    ReviewRequest,
    ReviewStatus,
)
from app.integrations.devin_sessions import (
    DevinSessionClient,
    LiveDevinSessionClient,
    SessionSnapshot,
    SessionStatus,
)
from app.integrations.github_client import (
    GitHubClient,
    LiveGitHubClient,
    StaticTokenProvider,
)
from app.integrations.github_commands import (
    PostIssueComment,
    RequestReviewers,
    format_reproduction_outcome_comment,
)
from app.integrations.repository import SUPERSET_REPOSITORY, TargetCommit
from app.integrations.tasks import (
    DEFAULT_ALLOWED_CAPABILITIES,
    CapabilityBudget,
    ClassificationOutput,
    FixOutput,
    ReproductionOutput,
    TaskEnvelope,
    TaskKind,
    TaskPolicy,
    validate_result,
)
from app.integrations.transport import HttpxTransport

SYSTEM_LOGIN = "relay-worker"


def _required_secret(name: str) -> str:
    inline = os.environ.get(name)
    file_name = os.environ.get(f"{name}_FILE")
    if inline and file_name:
        raise RuntimeError(f"configure only one of {name} or {name}_FILE")
    if file_name:
        path = Path(file_name)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"cannot read {name}_FILE") from error
    else:
        value = (inline or "").strip()
    if not value:
        raise RuntimeError(f"missing required live credential: {name} or {name}_FILE")
    return value


def _optional_secret(name: str) -> str | None:
    inline = os.environ.get(name)
    file_name = os.environ.get(f"{name}_FILE")
    if inline and file_name:
        raise RuntimeError(f"configure only one of {name} or {name}_FILE")
    if file_name:
        try:
            value = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"cannot read {name}_FILE") from error
        return value or None
    return inline.strip() if inline and inline.strip() else None


@dataclass
class LiveWorkerRuntime:
    service: TransitionService
    uow_factory: UnitOfWorkFactory
    github: GitHubClient
    devin: DevinSessionClient
    review: DevinReviewClient
    default_branch: str = "master"

    def execute(self, uow: UnitOfWork, job: Job) -> None:
        if job.issue_id is None:
            return
        issue = uow.get_issue(job.issue_id)
        if issue is None:
            raise RuntimeError("job issue is missing")
        if job.issue_revision is not None and job.issue_revision != issue.revision:
            return
        if job.kind == JobKind.CLASSIFY:
            self._start_classification(uow, issue, job)
            return
        if job.kind == JobKind.PUBLISH_QUESTIONS:
            self._publish_questions(uow, issue)
            return
        if job.kind == JobKind.START_REPRODUCTION:
            self._start_existing_session(uow, issue, job, TaskKind.REPRODUCTION)
            return
        if job.kind == JobKind.START_FIX:
            self._apply(uow, job, EventType.FIX_SESSION_STARTED, dict(job.payload))
            issue = self._issue(uow, job)
            self._start_existing_session(uow, issue, job, TaskKind.FIX)
            return
        if job.kind == JobKind.TRIGGER_DEVIN_REVIEW:
            pr = self._pull_request(uow, issue.id, job)
            self.review.trigger_review(
                ReviewRequest(
                    pull_request_number=pr.number,
                    head_commit=TargetCommit(sha=pr.head_sha),
                )
            )
            return
        if job.kind == JobKind.RESOLVE_REVIEWERS:
            self._resolve_reviewers(uow, issue, job)
            return
        if job.kind == JobKind.REQUEST_REVIEWERS:
            pr = self._pull_request(uow, issue.id, job)
            reviewers = self._string_tuple(job.payload.get("reviewers"))
            teams = self._string_tuple(job.payload.get("teams"))
            self.github.execute(
                RequestReviewers(
                    repository=SUPERSET_REPOSITORY,
                    pull_request_number=pr.number,
                    reviewers=reviewers,
                    team_reviewers=teams,
                )
            )
            return
        if job.kind == JobKind.REMINDER:
            self._apply(uow, job, EventType.REMINDER_ELAPSED, dict(job.payload))
            return
        if job.kind == JobKind.PUBLISH_REMINDER:
            number = job.payload.get("reminder_number")
            fields = ", ".join(self._string_tuple(job.payload.get("fields")))
            self.github.execute(
                PostIssueComment(
                    repository=SUPERSET_REPOSITORY,
                    issue_number=issue.external_number,
                    body=(
                        f"Relay reminder {number}: reproduction context is still needed"
                        f" for {fields}. Please reply when you can; the Relay record may"
                        " become inactive, but GitHub issue closure remains human-controlled."
                    ),
                )
            )
            return
        if job.kind == JobKind.INACTIVITY:
            self._apply(uow, job, EventType.INACTIVITY_ELAPSED, dict(job.payload))
            return
        if job.kind == JobKind.CANCEL_SESSION:
            session = self._session(uow, job)
            if session.external_session_id:
                self.devin.cancel_session(session.external_session_id)
            return
        if job.kind == JobKind.DELIVER_SESSION_MESSAGE:
            self._deliver_message(uow, job)
            return
        if job.kind in {
            JobKind.ESCALATE_OWNER,
            JobKind.SAFE_ALTERNATIVE_REVIEW,
            JobKind.RECOVERY,
            JobKind.PRIVATE_SECURITY_TASK,
        }:
            return
        raise RuntimeError(f"unsupported live job kind: {job.kind.value}")

    def sync(self) -> None:
        with self.uow_factory() as uow:
            sessions = [
                session
                for session in uow.list_sessions()
                if session.external_session_id
                and session.state in {SessionState.RUNNING, SessionState.NEEDS_ATTENTION}
            ]
        for session in sessions:
            self._sync_session(session.id)
        self._sync_reviews()

    def _start_classification(self, uow: UnitOfWork, issue: Issue, job: Job) -> None:
        existing = [
            session
            for session in uow.list_sessions(issue_id=issue.id)
            if session.kind == SessionKind.TRIAGE
            and session.issue_revision == issue.revision
            and session.external_session_id is not None
        ]
        if existing:
            return
        target = self._target_commit(uow, issue)
        now = self.service.clock.now()
        session = AgentSession(
            issue_id=issue.id,
            issue_revision=issue.revision,
            kind=SessionKind.TRIAGE,
            title=f"Classify {issue.key}",
            state=SessionState.RUNNING,
            target_commit=target.sha,
            budget=SessionBudget(
                wall_clock_seconds=900,
                max_retries=1,
                allowed_capabilities=["read_issue_context", "read_superset_repository"],
                max_output_bytes=262_144,
            ),
            workspace_released=False,
            workspace_name=f"superset-triage-{issue.external_number}",
            trigger="issue intake",
            correlation_id=issue.correlation_id,
            started_at=now,
            last_heartbeat_at=now,
            created_at=now,
            updated_at=now,
        )
        uow.add_session(session)
        self._create_external(uow, issue, session)

    def _start_existing_session(
        self, uow: UnitOfWork, issue: Issue, job: Job, kind: TaskKind
    ) -> None:
        session = self._session(uow, job)
        if session.external_session_id is not None:
            return
        if session.target_commit is None:
            session.target_commit = self._target_commit(uow, issue).sha
        self._create_external(uow, issue, session, kind=kind)

    def _create_external(
        self,
        uow: UnitOfWork,
        issue: Issue,
        session: AgentSession,
        *,
        kind: TaskKind | None = None,
    ) -> None:
        task = self._task(uow, issue, session, kind=kind)
        snapshot = self.devin.create_session(
            task,
            reporter_context=self._reporter_context(uow, issue),
        )
        self._attach_snapshot(session, snapshot)
        session.current_action = "Devin session accepted"
        session.progress_source = "devin-api"
        session.progress_synced_at = self.service.clock.now()
        uow.save_session(session)
        uow.add_session_event(
            SessionEvent(
                session_id=session.id,
                label="Devin session created",
                detail=f"Bound to {task.target_commit.short_sha}",
                created_at=self.service.clock.now(),
            )
        )

    def _sync_session(self, session_id: uuid.UUID) -> None:
        with self.uow_factory() as uow:
            session = uow.get_session(session_id)
            if session is None or session.external_session_id is None:
                return
            issue = uow.get_issue(session.issue_id)
            if issue is None:
                return
            try:
                snapshot = self.devin.get_session(session.external_session_id)
                self._attach_snapshot(session, snapshot)
                self._sync_conversation(uow, session)
                if snapshot.status == SessionStatus.COMPLETED:
                    self._complete_session(uow, issue, session)
                elif snapshot.status in {
                    SessionStatus.FAILED,
                    SessionStatus.CANCELLED,
                }:
                    self._fail_session(
                        uow,
                        issue,
                        session,
                        f"Devin session ended as {snapshot.status.value}",
                    )
                elif snapshot.status == SessionStatus.NEEDS_ATTENTION:
                    session.state = SessionState.NEEDS_ATTENTION
                    session.current_action = "Devin needs operator attention"
                    uow.save_session(session)
                else:
                    session.state = SessionState.RUNNING
                    session.current_action = "Devin session running"
                    uow.save_session(session)
            except Exception as error:
                self._fail_session(uow, issue, session, f"{type(error).__name__}: {error}")

    def _complete_session(
        self, uow: UnitOfWork, issue: Issue, session: AgentSession
    ) -> None:
        task = self._task(uow, issue, session)
        result = self.devin.collect_result(session.external_session_id or "", task)
        validate_result(
            task=task,
            result=result,
            current_issue_revision=issue.revision,
            received_at=self.service.clock.now(),
            policy=TaskPolicy(max_wall_seconds=5_400),
        )
        if isinstance(result.payload, ClassificationOutput):
            missing_fields: list[dict[str, object]] = []
            if result.payload.classification == "needs_information":
                missing_fields.append(
                    {
                        "field": "reproduction_context",
                        "prompt": result.payload.rationale,
                        "why_it_matters": (
                            "Relay needs portable context before isolated reproduction."
                        ),
                        "safe_example": (
                            "Version, minimal steps, expected result, and actual result."
                        ),
                    }
                )
            self._apply_session(
                uow,
                session,
                EventType.CLASSIFICATION_RESULT,
                {
                    "category": result.payload.classification,
                    "security_signal": result.payload.classification == "suspected_security",
                    "missing_fields": missing_fields,
                },
            )
        elif isinstance(result.payload, ReproductionOutput):
            payload = {
                "session_id": str(session.id),
                "reproduced": result.payload.reproduced,
                "observed_behavior": result.payload.observed_behavior,
                "expected_behavior": result.payload.target_behavior,
                "control_behavior": result.payload.control_behavior,
                "attempts": result.payload.attempts,
                "evidence": [
                    {
                        "kind": "run_log",
                        "title": "Observed behavior",
                        "summary": result.payload.observed_behavior,
                    },
                    {
                        "kind": "result_matrix",
                        "title": "Target and control behavior",
                        "summary": (
                            f"Target: {result.payload.target_behavior}; "
                            f"control: {result.payload.control_behavior}"
                        ),
                    },
                ],
            }
            self._apply_session(uow, session, EventType.REPRODUCTION_RESULT, payload)
            self.github.execute(
                PostIssueComment(
                    repository=SUPERSET_REPOSITORY,
                    issue_number=issue.external_number,
                    body=format_reproduction_outcome_comment(
                        reproduced=result.payload.reproduced,
                        observed_behavior=result.payload.observed_behavior,
                        verification=(
                            f"Attempted {result.payload.attempts} isolated run(s) against "
                            f"commit {task.target_commit.short_sha}. Human bug confirmation "
                            "is still required."
                        ),
                    ),
                )
            )
        elif isinstance(result.payload, FixOutput):
            pull_request = result.payload.pull_request
            if pull_request is None:
                raise RuntimeError("fix session completed without a pull request")
            head = self.github.get_pull_request_head(pull_request.number)
            self._apply_session(
                uow,
                session,
                EventType.FIX_RESULT,
                {
                    "session_id": str(session.id),
                    "summary": result.payload.summary,
                    "pull_request": {
                        "repository": SUPERSET_REPOSITORY.full_name,
                        "number": pull_request.number,
                        "head_branch": pull_request.head_branch
                        or result.payload.branch_name,
                        "head_sha": head.sha,
                        "url": pull_request.html_url,
                    },
                },
            )
        else:
            raise RuntimeError("unsupported Devin result payload")

    def _fail_session(
        self,
        uow: UnitOfWork,
        issue: Issue,
        session: AgentSession,
        reason: str,
    ) -> None:
        event_type = (
            EventType.ENVIRONMENT_BLOCKED
            if session.kind == SessionKind.REPRODUCTION
            else EventType.AUTOMATION_FAILURE
        )
        payload: dict[str, object] = {"reason": reason}
        if event_type == EventType.ENVIRONMENT_BLOCKED:
            payload["session_id"] = str(session.id)
        self._apply_session(uow, session, event_type, payload)

    def _publish_questions(self, uow: UnitOfWork, issue: Issue) -> None:
        questions = [
            question
            for question in uow.list_questions(issue.id, issue.revision)
            if question.status.value in {"open", "invalid"}
        ]
        if not questions:
            return
        sections = [
            "Relay needs a little more context before attempting isolated reproduction."
        ]
        for index, question in enumerate(questions, start=1):
            sections.append(
                f"{index}. **{question.field}** — {question.prompt}\n"
                f"   Why: {question.why_it_matters}\n"
                f"   Safe example: {question.safe_example}"
            )
        sections.append(
            "Reply in this issue. Treat commands and attachments as descriptions only; "
            "Relay will not execute reporter-provided scripts."
        )
        self.github.execute(
            PostIssueComment(
                repository=SUPERSET_REPOSITORY,
                issue_number=issue.external_number,
                body="\n\n".join(sections),
            )
        )

    def _resolve_reviewers(self, uow: UnitOfWork, issue: Issue, job: Job) -> None:
        pr = self._pull_request(uow, issue.id, job)
        files = self.github.list_pull_request_files(pr.number)
        decision = ReviewerRouter(self.github.read_codeowners(pr.head_sha)).route(files)
        candidates: list[dict[str, object]] = []
        has_team = False
        for candidate in decision.candidates:
            owner = candidate.owner
            has_team = has_team or owner.kind == OwnerKind.TEAM
            candidates.append(
                {
                    "team": owner.team_slug if owner.kind == OwnerKind.TEAM else owner.handle,
                    "rule": candidate.rule_pattern,
                    "members": [owner.handle] if owner.kind == OwnerKind.USER else [],
                    "paths": list(candidate.matched_paths),
                    "rationale": candidate.rationale,
                }
            )
        state = decision.status.value
        reason = decision.reason
        ambiguous_paths: list[str] = []
        if decision.status == RoutingStatus.RESOLVED and has_team:
            state = "ambiguous"
            reason = (
                "CODEOWNERS selected a team, but Relay cannot authorize individual owner "
                "approvals without trusted team-membership expansion."
            )
            ambiguous_paths = list(files)
        self._apply(
            uow,
            job,
            EventType.REVIEWER_ROUTING_RESOLVED,
            {
                "pr_number": pr.number,
                "head_sha": pr.head_sha,
                "state": state,
                "candidates": candidates,
                "unowned_paths": list(decision.unowned_paths),
                "ambiguous_paths": ambiguous_paths,
                "rationale": reason,
            },
        )

    def _sync_reviews(self) -> None:
        with self.uow_factory() as uow:
            issues = uow.list_issues()
            pending: list[tuple[uuid.UUID, int, str]] = []
            for issue in issues:
                completed_jobs = {
                    (job.kind, job.payload.get("pr_number"), job.payload.get("head_sha"))
                    for job in uow.list_jobs(issue.id)
                    if job.status == JobStatus.DONE
                }
                for pr in uow.list_pull_requests(issue.id):
                    if (
                        pr.devin_review.value == "pending"
                        and (
                            JobKind.TRIGGER_DEVIN_REVIEW,
                            pr.number,
                            pr.head_sha,
                        )
                        in completed_jobs
                    ):
                        pending.append((issue.id, pr.number, pr.head_sha))
        for issue_id, number, head_sha in pending:
            request = ReviewRequest(
                pull_request_number=number,
                head_commit=TargetCommit(sha=head_sha),
            )
            run = self.review.latest_review(request)
            if run is None or not run.status.is_terminal:
                continue
            verdict = "failed"
            findings = 0
            if run.status == ReviewStatus.COMPLETED:
                review_findings = self.review.list_findings(request)
                findings = len(review_findings)
                verdict = "findings" if findings else "passed"
            with self.uow_factory() as uow:
                current_issue = uow.get_issue(issue_id)
                if current_issue is None:
                    continue
                job = next(
                    (
                        candidate
                        for candidate in uow.list_jobs(issue_id)
                        if candidate.kind == JobKind.TRIGGER_DEVIN_REVIEW
                        and candidate.payload.get("pr_number") == number
                        and candidate.payload.get("head_sha") == head_sha
                    ),
                    None,
                )
                if job is None:
                    continue
                self._apply(
                    uow,
                    job,
                    EventType.DEVIN_REVIEW_COMPLETED,
                    {
                        "pr_number": number,
                        "head_sha": head_sha,
                        "verdict": verdict,
                        "findings": findings,
                        "url": (
                            f"https://app.devin.ai/review/"
                            f"{SUPERSET_REPOSITORY.full_name}/pull/{number}"
                        ),
                    },
                    suffix="sync",
                )

    def _deliver_message(self, uow: UnitOfWork, job: Job) -> None:
        session = self._session(uow, job)
        if session.external_session_id is None:
            raise RuntimeError("session has no external Devin identifier")
        message_id = self._payload_uuid(job, "message_id")
        message = next(
            (entry for entry in uow.list_messages(session.id) if entry.id == message_id),
            None,
        )
        if message is None:
            raise RuntimeError("queued session message is missing")
        self.devin.send_message(session.external_session_id, message.body)
        message.delivered = True
        uow.save_message(message)

    def _sync_conversation(self, uow: UnitOfWork, session: AgentSession) -> None:
        if session.external_session_id is None:
            return
        _, remote = self.devin.fetch_conversation(session.external_session_id)
        existing = {
            (message.author.value, message.created_at, message.body)
            for message in uow.list_messages(session.id)
        }
        for message in remote:
            author = (
                ConversationAuthor.DEVIN
                if message.author.lower() in {"devin", "assistant", "agent"}
                else ConversationAuthor.RELAY
            )
            key = (author.value, message.created_at, message.text)
            if key in existing:
                continue
            uow.add_message(
                ConversationMessage(
                    session_id=session.id,
                    author=author,
                    author_login=message.author,
                    body=message.text,
                    delivered=True,
                    created_at=message.created_at,
                )
            )

    def _task(
        self,
        uow: UnitOfWork,
        issue: Issue,
        session: AgentSession,
        *,
        kind: TaskKind | None = None,
    ) -> TaskEnvelope:
        task_kind = kind or {
            SessionKind.TRIAGE: TaskKind.CLASSIFICATION,
            SessionKind.REPRODUCTION: TaskKind.REPRODUCTION,
            SessionKind.FIX: TaskKind.FIX,
        }[session.kind]
        target = (
            TargetCommit(sha=session.target_commit)
            if session.target_commit
            else self._target_commit(uow, issue)
        )
        objective = {
            TaskKind.CLASSIFICATION: (
                f"Classify issue #{issue.external_number}. Identify suspected security reports "
                "and whether portable reproduction context is missing."
            ),
            TaskKind.REPRODUCTION: (
                f"Reproduce issue #{issue.external_number} against the immutable commit. "
                "Return observed, target, and control behavior from isolated tests."
            ),
            TaskKind.FIX: (
                f"Implement the minimal authorized fix for issue #{issue.external_number}, "
                "add regression coverage, and open a draft pull request."
            ),
            TaskKind.EVIDENCE_PACKET: "Assemble a bounded evidence packet.",
        }[task_kind]
        return TaskEnvelope(
            task_id=session.id,
            issue_id=issue.id,
            issue_revision=issue.revision,
            kind=task_kind,
            created_at=session.created_at,
            target_commit=target,
            budget=CapabilityBudget(
                wall_seconds=session.budget.wall_clock_seconds,
                retry_limit=session.budget.max_retries,
                max_output_bytes=session.budget.max_output_bytes,
            ),
            allowed_capabilities=DEFAULT_ALLOWED_CAPABILITIES[task_kind],
            objective=objective,
        )

    def _reporter_context(self, uow: UnitOfWork, issue: Issue) -> str:
        revisions = uow.list_revisions(issue.id)
        revision = next(
            (entry for entry in reversed(revisions) if entry.revision == issue.revision),
            revisions[-1] if revisions else None,
        )
        lines = [f"Title: {issue.title}"]
        if revision is not None:
            lines.append(f"Body:\n{revision.body}")
        answers = [
            f"{question.field}: {question.answer}"
            for question in uow.list_questions(issue.id, issue.revision)
            if question.answer
        ]
        if answers:
            lines.append("Reporter answers:\n" + "\n".join(answers))
        return "\n\n".join(lines)

    def _target_commit(self, uow: UnitOfWork, issue: Issue) -> TargetCommit:
        revisions = uow.list_revisions(issue.id)
        revision = next(
            (entry for entry in reversed(revisions) if entry.revision == issue.revision),
            None,
        )
        if revision is not None and revision.target_commit:
            return TargetCommit(sha=revision.target_commit)
        return self.github.get_branch_head(self.default_branch)

    def _attach_snapshot(
        self, session: AgentSession, snapshot: SessionSnapshot
    ) -> None:
        session.external_session_id = snapshot.session_id
        session.external_session_url = snapshot.links.session_url
        session.external_desktop_url = snapshot.links.desktop_url
        session.last_heartbeat_at = snapshot.updated_at
        session.updated_at = snapshot.updated_at
        session.workspace_released = snapshot.workspace_status.value == "released"
        if session.workspace_released:
            session.workspace_released_at = snapshot.updated_at

    def _apply_session(
        self,
        uow: UnitOfWork,
        session: AgentSession,
        event_type: EventType,
        payload: dict[str, object],
    ) -> None:
        event = Event(
            issue_id=session.issue_id,
            source="worker",
            delivery_id=f"session:{session.id}:{event_type.value}",
            type=event_type,
            actor=self._actor(
                ActorRole.SYSTEM
                if event_type == EventType.REVIEWER_ROUTING_RESOLVED
                else ActorRole.AGENT
            ),
            payload=payload,
            correlation_id=session.correlation_id,
        )
        self.service.apply(uow, event)

    def _apply(
        self,
        uow: UnitOfWork,
        job: Job,
        event_type: EventType,
        payload: dict[str, object],
        *,
        suffix: str = "effect",
    ) -> None:
        event = Event(
            issue_id=job.issue_id,
            source="worker",
            delivery_id=f"{job.idempotency_key}:{suffix}",
            type=event_type,
            actor=self._actor(ActorRole.SYSTEM),
            payload=payload,
            correlation_id=job.correlation_id,
        )
        self.service.apply(uow, event)

    @staticmethod
    def _actor(role: ActorRole) -> Actor:
        return Actor(role=role, login=SYSTEM_LOGIN)

    @staticmethod
    def _string_tuple(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return ()
        return tuple(value)

    @staticmethod
    def _payload_uuid(job: Job, field: str) -> uuid.UUID:
        value = job.payload.get(field)
        if not isinstance(value, str):
            raise RuntimeError(f"job payload.{field} is required")
        return uuid.UUID(value)

    def _session(self, uow: UnitOfWork, job: Job) -> AgentSession:
        session = uow.get_session(self._payload_uuid(job, "session_id"))
        if session is None:
            raise RuntimeError("job session is missing")
        return session

    @staticmethod
    def _issue(uow: UnitOfWork, job: Job) -> Issue:
        issue = uow.get_issue(job.issue_id) if job.issue_id is not None else None
        if issue is None:
            raise RuntimeError("job issue is missing")
        return issue

    @staticmethod
    def _pull_request(
        uow: UnitOfWork, issue_id: uuid.UUID, job: Job
    ) -> PullRequestState:
        number = job.payload.get("pr_number")
        head_sha = job.payload.get("head_sha")
        for pr in uow.list_pull_requests(issue_id):
            if pr.number == number and pr.head_sha == head_sha:
                return pr
        raise RuntimeError("job pull request binding is stale")


def live_runtime_from_env(
    service: TransitionService, uow_factory: UnitOfWorkFactory
) -> LiveWorkerRuntime:
    github_token = _required_secret("GITHUB_TOKEN")
    devin_token = _required_secret("DEVIN_API_TOKEN")
    review_token = _optional_secret("DEVIN_REVIEW_TOKEN") or devin_token
    org_id = os.environ.get("DEVIN_ORG_ID", "").strip()
    if not org_id:
        raise RuntimeError("missing required live configuration: DEVIN_ORG_ID")
    timeout = float(os.environ.get("RELAY_HTTP_TIMEOUT_SECONDS", "30"))
    retries = int(os.environ.get("RELAY_HTTP_RETRIES", "2"))
    transport = HttpxTransport(timeout_seconds=timeout, retries=retries)
    task_policy = TaskPolicy(max_wall_seconds=5_400)
    return LiveWorkerRuntime(
        service=service,
        uow_factory=uow_factory,
        github=LiveGitHubClient(
            transport=transport,
            token_provider=StaticTokenProvider(github_token),
        ),
        devin=LiveDevinSessionClient(
            transport=transport,
            token_provider=StaticTokenProvider(devin_token),
            org_id=org_id,
            policy=task_policy,
        ),
        review=LiveDevinReviewClient(
            transport=transport,
            token_provider=StaticTokenProvider(review_token),
        ),
        default_branch=os.environ.get("GITHUB_DEFAULT_BRANCH", "master"),
    )
