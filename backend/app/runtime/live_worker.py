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

import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from app.api.controller_gate import GatePolicy, evaluate_issue_event, log_decision
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
from app.domain.states import ActorRole, EventType, IssueState, SessionKind, SessionState
from app.domain.transitions import TransitionService
from app.integrations.codeowners import OwnerKind, ReviewerRouter, RoutingStatus
from app.integrations.devin_automations import (
    AUTOMATION_KINDS,
    DEFAULT_TRIGGER_LABEL,
    GITHUB_ISSUES_EVENT_TYPE,
    AutomationHandle,
    AutomationSecretStore,
    AutomationSessionRow,
    DevinAutomationClient,
    InMemoryAutomationSecretStore,
    LiveDevinAutomationClient,
    NativeTriageOutput,
    native_issue_number,
    parse_native_triage_output,
)
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
NATIVE_SOURCE = "devin-automation"
LOGGER = logging.getLogger("relay.native_intake")


@dataclass
class NativeIntakeState:
    """What the worker last saw when polling the native reproduction automation."""

    automation_id: str | None = None
    last_polled_at: datetime | None = None
    last_error: str | None = None
    ignored_session_ids: set[str] = field(default_factory=set)


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
    automations: DevinAutomationClient | None = None
    gate_policy: GatePolicy = field(default_factory=GatePolicy.from_env)
    trigger_label: str = DEFAULT_TRIGGER_LABEL
    native_intake: NativeIntakeState = field(default_factory=NativeIntakeState)

    def automation_handles(self) -> dict[TaskKind, AutomationHandle]:
        """Find-or-create Relay's reproduction and fix automations."""
        if self.automations is None:
            return {}
        now = self.service.clock.now()
        return {
            kind: self.automations.ensure_automation(kind, now=now) for kind in AUTOMATION_KINDS
        }

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
        self._adopt_native_sessions()
        self._link_dispatched_sessions()
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

    # ------------------------------------------------------- native intake

    def _adopt_native_sessions(self) -> None:
        """Enroll issues from sessions Devin's own GitHub trigger started.

        The reproduction automation fires on ``github:issues`` inside Devin, so
        Relay learns about new work by listing that automation's sessions. A
        session is adopted only once its structured output names a Superset
        issue; the issue is then re-checked against the deterministic gate
        and enrolled with the data read back from GitHub. Sessions that never
        identify an issue, or fail the gate, are left alone.
        """
        if self.automations is None:
            return
        now = self.service.clock.now()
        state = self.native_intake
        try:
            handle = self.automations.ensure_automation(TaskKind.REPRODUCTION, now=now)
            if not handle.native:
                return
            rows = self.automations.list_automation_sessions(handle.automation_id)
        except Exception as error:
            state.last_error = f"{type(error).__name__}: {error}"
            LOGGER.warning("native intake poll failed: %s", state.last_error)
            return
        state.automation_id = handle.automation_id
        state.last_polled_at = now
        state.last_error = None
        with self.uow_factory() as uow:
            known = {s.external_session_id for s in uow.list_sessions() if s.external_session_id}
        for row in sorted(rows, key=lambda r: r.created_at):
            if row.session_id in known or row.session_id in state.ignored_session_ids:
                continue
            number = native_issue_number(row.structured_output)
            if number is None:
                if row.is_terminal:
                    state.ignored_session_ids.add(row.session_id)
                    LOGGER.info(
                        "ignoring automation session %s: it never identified an issue",
                        row.session_id,
                    )
                continue
            try:
                adopted = self._adopt_native_session(handle, row, number, now)
            except Exception:
                LOGGER.exception("adopting automation session %s failed", row.session_id)
                continue
            if adopted is True:
                known.add(row.session_id)
            elif adopted is False:
                state.ignored_session_ids.add(row.session_id)
        self._expire_waiting_native_reproductions()

    def _expire_waiting_native_reproductions(self) -> None:
        with self.uow_factory() as uow:
            waiting = [
                s
                for s in uow.list_sessions()
                if self._is_native(s)
                and s.kind is SessionKind.REPRODUCTION
                and s.external_session_id is None
                and s.state is SessionState.RUNNING
            ]
        for session in waiting:
            self._expire_dispatch(session)

    def _adopt_native_session(
        self,
        handle: AutomationHandle,
        row: AutomationSessionRow,
        number: int,
        now: datetime,
    ) -> bool | None:
        """``True`` once linked, ``False`` to ignore for good, ``None`` to retry later."""
        snapshot = self.github.get_issue(number)
        policy = replace(
            self.gate_policy,
            trusted_label=self.gate_policy.trusted_label or self.trigger_label,
        )
        decision = evaluate_issue_event(
            policy,
            repository=SUPERSET_REPOSITORY.full_name,
            action="opened",
            issue_state=snapshot.state,
            labels=snapshot.labels,
            label_added=None,
            sender=snapshot.reporter_login,
        )
        log_decision(decision, f"devin:{row.session_id}")
        if not decision.accepted:
            return False
        with self.uow_factory() as uow:
            repo = uow.get_repository()
            issue = uow.get_issue_by_number(repo.id, number) if repo is not None else None
            if issue is not None:
                waiting = self._waiting_native_reproduction(uow, issue)
                if waiting is not None:
                    self._bind_native_row(waiting, handle, row, now)
                    uow.save_session(waiting)
                    uow.add_session_event(
                        SessionEvent(
                            session_id=waiting.id,
                            label="Devin session adopted",
                            detail=f"Automation {handle.automation_id} started {row.session_id}",
                            created_at=now,
                        )
                    )
                    return True
            if issue is None:
                event = Event(
                    issue_id=None,
                    source="devin",
                    delivery_id=f"automation:{row.session_id}",
                    type=EventType.ISSUE_OPENED,
                    actor=self._actor(ActorRole.SYSTEM),
                    payload={
                        "repository": SUPERSET_REPOSITORY.full_name,
                        "number": number,
                        "title": snapshot.title,
                        "body": snapshot.body,
                        "reporter": snapshot.reporter_login,
                        "labels": list(snapshot.labels),
                    },
                    correlation_id=f"devin:{row.session_id}",
                )
                issue = self.service.apply(uow, event).issue
                if issue is None:
                    raise RuntimeError("native enrollment returned no issue")
            if issue.state is not IssueState.TRIAGE:
                LOGGER.info(
                    "issue #%s is %s; not adopting automation session %s",
                    number,
                    issue.state.value,
                    row.session_id,
                )
                return False
            if any(
                s.kind == SessionKind.TRIAGE and s.issue_revision == issue.revision
                for s in uow.list_sessions(issue_id=issue.id)
            ):
                # Relay's own classification is underway; the reproduction it may
                # create will wait for this session, so look again next poll.
                return None
            target = self._target_commit(uow, issue)
            session = AgentSession(
                issue_id=issue.id,
                issue_revision=issue.revision,
                kind=SessionKind.TRIAGE,
                title=f"Triage {issue.key}",
                state=SessionState.RUNNING,
                target_commit=target.sha,
                budget=SessionBudget(
                    wall_clock_seconds=5_400,
                    max_retries=1,
                    allowed_capabilities=["read_issue_context", "read_superset_repository"],
                    max_output_bytes=262_144,
                ),
                workspace_released=False,
                workspace_name=f"superset-triage-{issue.external_number}",
                trigger=GITHUB_ISSUES_EVENT_TYPE,
                correlation_id=issue.correlation_id,
                created_at=row.created_at,
                updated_at=now,
            )
            self._bind_native_row(session, handle, row, now)
            uow.add_session(session)
            uow.add_session_event(
                SessionEvent(
                    session_id=session.id,
                    label="Devin session adopted",
                    detail=(
                        f"Started by automation {handle.automation_id} on"
                        f" {GITHUB_ISSUES_EVENT_TYPE} for #{number}"
                    ),
                    created_at=now,
                )
            )
        return True

    @staticmethod
    def _bind_native_row(
        session: AgentSession, handle: AutomationHandle, row: AutomationSessionRow, now: datetime
    ) -> None:
        session.automation_id = handle.automation_id
        session.dispatched_at = session.dispatched_at or row.created_at
        session.trigger = GITHUB_ISSUES_EVENT_TYPE
        session.external_session_id = row.session_id
        session.external_session_url = row.url
        session.current_action = "Devin session running"
        session.progress_source = NATIVE_SOURCE
        session.progress_synced_at = now
        session.started_at = session.started_at or row.created_at
        session.last_heartbeat_at = row.updated_at
        session.updated_at = now

    @staticmethod
    def _waiting_native_reproduction(uow: UnitOfWork, issue: Issue) -> AgentSession | None:
        return next(
            (
                s
                for s in uow.list_sessions(issue_id=issue.id)
                if s.kind is SessionKind.REPRODUCTION
                and s.trigger == GITHUB_ISSUES_EVENT_TYPE
                and s.external_session_id is None
                and s.state is SessionState.RUNNING
            ),
            None,
        )

    @staticmethod
    def _is_native(session: AgentSession) -> bool:
        return session.trigger == GITHUB_ISSUES_EVENT_TYPE and session.automation_id is not None

    # ----------------------------------------------------- inbox dispatch

    def _link_dispatched_sessions(self) -> None:
        """Bind inbox-dispatched Devin sessions to the Relay sessions that asked for them.

        Devin creates the session asynchronously after the inbox post, so the
        external id is unknown at dispatch time. Spawned sessions are matched
        by ``structured_output.task_id`` when the agent has already echoed it,
        otherwise oldest-unlinked to oldest-dispatched per automation. Native
        (``github:issues``) sessions never wait here: they are adopted by issue.
        """
        if self.automations is None:
            return
        with self.uow_factory() as uow:
            all_sessions = uow.list_sessions()
        known = {s.external_session_id for s in all_sessions if s.external_session_id}
        pending = sorted(
            (
                s
                for s in all_sessions
                if s.automation_id
                and s.external_session_id is None
                and s.dispatched_at is not None
                and s.state is SessionState.RUNNING
                and not self._is_native(s)
            ),
            key=lambda s: (s.dispatched_at or s.created_at, s.created_at),
        )
        by_automation: dict[str, list[AgentSession]] = {}
        for session in pending:
            assert session.automation_id is not None
            by_automation.setdefault(session.automation_id, []).append(session)
        for automation_id, waiting in by_automation.items():
            since = min(s.dispatched_at for s in waiting if s.dispatched_at is not None)
            try:
                spawned = [
                    snap
                    for snap in self.automations.list_spawned_sessions(automation_id, since=since)
                    if snap.session_id not in known
                ]
            except Exception as error:
                self._mark_dispatch_wait(waiting, f"{type(error).__name__}: {error}")
                continue
            by_task = {
                str(snap.structured_output.get("task_id")): snap
                for snap in spawned
                if snap.structured_output is not None
                and isinstance(snap.structured_output.get("task_id"), str)
            }
            unmatched = [snap for snap in spawned if snap not in by_task.values()]
            for session in waiting:
                snapshot = by_task.get(str(session.id))
                if snapshot is None and unmatched:
                    snapshot = unmatched.pop(0)
                if snapshot is None:
                    self._expire_dispatch(session)
                    continue
                known.add(snapshot.session_id)
                self._bind_spawned(session.id, snapshot)

    def _mark_dispatch_wait(self, waiting: list[AgentSession], detail: str) -> None:
        with self.uow_factory() as uow:
            for pending in waiting:
                session = uow.get_session(pending.id)
                if session is None:
                    continue
                session.current_action = f"Waiting for Devin automation ({detail})"
                uow.save_session(session)

    def _expire_dispatch(self, pending: AgentSession) -> None:
        now = self.service.clock.now()
        if pending.dispatched_at is None:
            return
        waited = (now - pending.dispatched_at).total_seconds()
        if waited <= pending.budget.wall_clock_seconds:
            return
        with self.uow_factory() as uow:
            session = uow.get_session(pending.id)
            issue = uow.get_issue(pending.issue_id)
            if session is None or issue is None:
                return
            self._fail_session(
                uow,
                issue,
                session,
                "Devin automation did not start a session within the budget",
            )

    def _bind_spawned(self, session_id: uuid.UUID, snapshot: SessionSnapshot) -> None:
        with self.uow_factory() as uow:
            session = uow.get_session(session_id)
            if session is None or session.external_session_id is not None:
                return
            self._attach_snapshot(session, snapshot)
            session.current_action = "Devin session accepted"
            session.progress_source = "devin-automation"
            session.progress_synced_at = self.service.clock.now()
            uow.save_session(session)
            uow.add_session_event(
                SessionEvent(
                    session_id=session.id,
                    label="Devin session created",
                    detail=f"Started by automation {session.automation_id}",
                    created_at=self.service.clock.now(),
                )
            )

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
        if session.external_session_id is not None or session.automation_id is not None:
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
        if task.kind in AUTOMATION_KINDS:
            if self.automations is None:
                raise RuntimeError(
                    f"{task.kind.value} sessions are launched only through Devin Automations"
                )
            handle = self.automations.ensure_automation(task.kind, now=self.service.clock.now())
            if handle.native:
                self._await_native_session(uow, issue, session, handle)
            else:
                self._dispatch_to_automation(uow, issue, session, task)
            return
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

    def _await_native_session(
        self, uow: UnitOfWork, issue: Issue, session: AgentSession, handle: AutomationHandle
    ) -> None:
        """Park a Relay-started reproduction until Devin's ``github:issues`` session shows up.

        Relay cannot post to a native automation; the session Devin starts for
        the same issue is bound to this record by issue number during adoption.
        """
        now = self.service.clock.now()
        session.automation_id = handle.automation_id
        session.dispatched_at = now
        session.trigger = GITHUB_ISSUES_EVENT_TYPE
        session.state = SessionState.RUNNING
        session.current_action = (
            f"Waiting for Devin's {GITHUB_ISSUES_EVENT_TYPE} automation session"
            f" for #{issue.external_number}"
        )
        session.progress_source = NATIVE_SOURCE
        session.progress_synced_at = now
        session.updated_at = now
        uow.save_session(session)
        uow.add_session_event(
            SessionEvent(
                session_id=session.id,
                label="Awaiting Devin automation",
                detail=(
                    f"Automation {handle.automation_id} starts sessions from Devin's GitHub"
                    f" integration; Relay links the one for #{issue.external_number}."
                ),
                created_at=now,
            )
        )

    def _dispatch_to_automation(
        self, uow: UnitOfWork, issue: Issue, session: AgentSession, task: TaskEnvelope
    ) -> None:
        assert self.automations is not None
        now = self.service.clock.now()
        handle = self.automations.ensure_automation(task.kind, now=now)
        receipt = self.automations.dispatch(
            handle,
            task,
            reporter_context=self._reporter_context(uow, issue),
            now=now,
        )
        session.automation_id = receipt.automation_id
        session.dispatched_at = receipt.dispatched_at
        session.state = SessionState.RUNNING
        session.started_at = session.started_at or now
        session.last_heartbeat_at = now
        session.current_action = "Dispatched to Devin automation"
        session.next_checkpoint = "Devin starts the session"
        session.progress_source = "devin-automation"
        session.progress_synced_at = now
        uow.save_session(session)
        uow.add_session_event(
            SessionEvent(
                session_id=session.id,
                label="Dispatched to Devin automation",
                detail=(
                    f"{task.kind.value} automation {receipt.automation_id}, "
                    f"bound to {task.target_commit.short_sha}"
                ),
                created_at=now,
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
                snapshot = self.devin.get_session(
                    session.external_session_id,
                    task=self._task(uow, issue, session) if session.automation_id else None,
                )
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

    def _complete_session(self, uow: UnitOfWork, issue: Issue, session: AgentSession) -> None:
        task = self._task(uow, issue, session)
        external_id = session.external_session_id or ""
        if self._is_native(session):
            self._complete_native_session(uow, issue, session, task)
            return
        if session.automation_id is not None:
            # Automation-spawned sessions carry no per-task tag, so the only
            # identity proof is the task_id the agent echoes in its output.
            echoed = (self.devin.get_session(external_id, task=task).structured_output or {}).get(
                "task_id"
            )
            if echoed is not None and str(echoed) != str(task.task_id):
                raise RuntimeError(
                    f"Devin session {external_id} reported task {echoed}, expected {task.task_id}"
                )
        result = self.devin.collect_result(external_id, task)
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
                        "head_branch": pull_request.head_branch or result.payload.branch_name,
                        "head_sha": head.sha,
                        "url": pull_request.html_url,
                    },
                },
            )
        else:
            raise RuntimeError("unsupported Devin result payload")

    def _complete_native_session(
        self, uow: UnitOfWork, issue: Issue, session: AgentSession, task: TaskEnvelope
    ) -> None:
        """Apply the triage + reproduction outcome of a natively triggered session.

        One Devin session covers both phases, so its output is replayed as a
        classification result and, when the context gate passed, as the
        reproduction result of the session the lifecycle created for it.
        """
        external_id = session.external_session_id or ""
        snapshot = self.devin.get_session(external_id, task=task)
        output = parse_native_triage_output(snapshot.structured_output)
        if output.issue_number != issue.external_number:
            raise RuntimeError(
                f"Devin session {external_id} reported issue #{output.issue_number},"
                f" expected #{issue.external_number}"
            )
        if session.kind is SessionKind.REPRODUCTION:
            self._apply_native_reproduction(uow, issue, session, output, task.target_commit)
            return
        if session.kind is not SessionKind.TRIAGE:
            raise RuntimeError("native sessions only cover triage and reproduction")
        if output.classification is None:
            raise RuntimeError(f"Devin session {external_id} ended without a classification")
        category = output.effective_classification
        missing_fields: list[dict[str, object]] = [
            {
                "field": item.field,
                "prompt": item.prompt,
                "why_it_matters": item.why_it_matters,
                "safe_example": item.safe_example,
            }
            for item in output.missing_fields
        ]
        if category == "needs_information" and not missing_fields:
            missing_fields.append(
                {
                    "field": "reproduction_context",
                    "prompt": output.rationale or "Please add reproduction context.",
                    "why_it_matters": (
                        "Relay needs portable context before isolated reproduction."
                    ),
                    "safe_example": "Version, minimal steps, expected result, and actual result.",
                }
            )
        payload: dict[str, object] = {
            "category": category,
            "security_signal": category == "suspected_security",
            "missing_fields": missing_fields,
        }
        if output.duplicate_of is not None:
            payload["duplicate_of"] = output.duplicate_of
        self._apply_session(uow, session, EventType.CLASSIFICATION_RESULT, payload)
        self._finish_native_triage(uow, session, output)
        refreshed = uow.get_issue(issue.id)
        if refreshed is None or refreshed.state is not IssueState.REPRODUCING:
            return
        reproduction = next(
            (
                s
                for s in uow.list_sessions(issue_id=issue.id)
                if s.kind is SessionKind.REPRODUCTION
                and s.issue_revision == refreshed.revision
                and s.external_session_id is None
                and s.state is SessionState.RUNNING
            ),
            None,
        )
        if reproduction is None:
            return
        reproduction.automation_id = session.automation_id
        reproduction.dispatched_at = session.dispatched_at
        reproduction.trigger = GITHUB_ISSUES_EVENT_TYPE
        reproduction.target_commit = reproduction.target_commit or session.target_commit
        self._attach_snapshot(reproduction, snapshot)
        reproduction.progress_source = NATIVE_SOURCE
        reproduction.progress_synced_at = self.service.clock.now()
        uow.save_session(reproduction)
        uow.add_session_event(
            SessionEvent(
                session_id=reproduction.id,
                label="Devin session adopted",
                detail=f"Reproduction ran inside triage session {external_id}",
                created_at=self.service.clock.now(),
            )
        )
        self._apply_native_reproduction(
            uow,
            refreshed,
            reproduction,
            output,
            TargetCommit(sha=reproduction.target_commit) if reproduction.target_commit else None,
        )

    def _finish_native_triage(
        self, uow: UnitOfWork, session: AgentSession, output: NativeTriageOutput
    ) -> None:
        current = uow.get_session(session.id)
        if current is None or current.state is not SessionState.RUNNING:
            return
        now = self.service.clock.now()
        current.state = SessionState.COMPLETED
        current.finished_at = now
        current.workspace_released = True
        current.workspace_released_at = current.workspace_released_at or now
        current.workspace_name = None
        current.current_action = None
        current.next_checkpoint = None
        current.updated_at = now
        uow.save_session(current)
        completeness = (
            "unknown" if output.context_completeness is None else f"{output.context_completeness}%"
        )
        uow.add_session_event(
            SessionEvent(
                session_id=current.id,
                label="Triage complete",
                detail=(
                    f"Classified as {output.effective_classification};"
                    f" context completeness {completeness}"
                ),
                created_at=now,
            )
        )

    def _apply_native_reproduction(
        self,
        uow: UnitOfWork,
        issue: Issue,
        session: AgentSession,
        output: NativeTriageOutput,
        target: TargetCommit | None,
    ) -> None:
        reproduction = output.reproduction
        if reproduction is None:
            self._fail_session(
                uow, issue, session, "Devin session ended without reproduction evidence"
            )
            return
        payload = {
            "session_id": str(session.id),
            "reproduced": reproduction.reproduced,
            "observed_behavior": reproduction.observed_behavior,
            "expected_behavior": reproduction.target_behavior,
            "control_behavior": reproduction.control_behavior,
            "attempts": reproduction.attempts,
            "evidence": [
                {
                    "kind": "run_log",
                    "title": "Observed behavior",
                    "summary": reproduction.observed_behavior,
                },
                {
                    "kind": "result_matrix",
                    "title": "Target and control behavior",
                    "summary": (
                        f"Target: {reproduction.target_behavior}; "
                        f"control: {reproduction.control_behavior}"
                    ),
                },
            ],
        }
        self._apply_session(uow, session, EventType.REPRODUCTION_RESULT, payload)
        commit = reproduction.target_commit or (
            target.short_sha if target else "the default branch"
        )
        self.github.execute(
            PostIssueComment(
                repository=SUPERSET_REPOSITORY,
                issue_number=issue.external_number,
                body=format_reproduction_outcome_comment(
                    reproduced=reproduction.reproduced,
                    observed_behavior=reproduction.observed_behavior,
                    verification=(
                        f"Attempted {reproduction.attempts} isolated run(s) against "
                        f"{commit}. Human bug confirmation is still required."
                    ),
                ),
            )
        )

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
        sections = ["Relay needs a little more context before attempting isolated reproduction."]
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
        task_kind = (
            kind
            or {
                SessionKind.TRIAGE: TaskKind.CLASSIFICATION,
                SessionKind.REPRODUCTION: TaskKind.REPRODUCTION,
                SessionKind.FIX: TaskKind.FIX,
            }[session.kind]
        )
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

    def _attach_snapshot(self, session: AgentSession, snapshot: SessionSnapshot) -> None:
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
    def _pull_request(uow: UnitOfWork, issue_id: uuid.UUID, job: Job) -> PullRequestState:
        number = job.payload.get("pr_number")
        head_sha = job.payload.get("head_sha")
        for pr in uow.list_pull_requests(issue_id):
            if pr.number == number and pr.head_sha == head_sha:
                return pr
        raise RuntimeError("job pull request binding is stale")


def live_runtime_from_env(
    service: TransitionService,
    uow_factory: UnitOfWorkFactory,
    *,
    automation_secrets: AutomationSecretStore | None = None,
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
    devin = LiveDevinSessionClient(
        transport=transport,
        token_provider=StaticTokenProvider(devin_token),
        org_id=org_id,
        policy=task_policy,
    )
    gate_policy = GatePolicy.from_env()
    trigger_label = gate_policy.trusted_label or DEFAULT_TRIGGER_LABEL
    automations = LiveDevinAutomationClient(
        transport=transport,
        token_provider=StaticTokenProvider(devin_token),
        org_id=org_id,
        sessions=devin,
        secret_store=automation_secrets or InMemoryAutomationSecretStore(),
        trigger_label=trigger_label,
    )
    return LiveWorkerRuntime(
        service=service,
        uow_factory=uow_factory,
        github=LiveGitHubClient(
            transport=transport,
            token_provider=StaticTokenProvider(github_token),
        ),
        devin=devin,
        review=LiveDevinReviewClient(
            transport=transport,
            token_provider=StaticTokenProvider(review_token),
        ),
        default_branch=os.environ.get("GITHUB_DEFAULT_BRANCH", "master"),
        automations=automations,
        gate_policy=gate_policy,
        trigger_label=trigger_label,
    )
