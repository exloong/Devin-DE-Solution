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
from typing import Protocol

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
from app.domain.states import (
    TERMINAL_STATES,
    ActorRole,
    EventType,
    IssueState,
    SessionKind,
    SessionState,
)
from app.domain.transitions import TransitionService
from app.integrations.codeowners import OwnerKind, ReviewerRouter, RoutingStatus
from app.integrations.devin_automations import (
    AUTOMATION_KINDS,
    GITHUB_ISSUE_COMMENT_EVENT_TYPE,
    GITHUB_ISSUES_EVENT_TYPE,
    NATIVE_EVENT_TYPES,
    AutomationHandle,
    AutomationSessionRow,
    DevinAutomationClient,
    LiveDevinAutomationClient,
    NativeTriageOutput,
    native_issue_number,
    native_output_from_conversation,
    parse_native_fix_output,
    parse_native_triage_output,
    trigger_issue_number,
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
from app.integrations.github_commands import RequestReviewers
from app.integrations.json_values import JsonObject
from app.integrations.repository import SUPERSET_REPOSITORY, TargetCommit
from app.integrations.tasks import (
    DEFAULT_ALLOWED_CAPABILITIES,
    CapabilityBudget,
    TaskEnvelope,
    TaskKind,
    TaskPolicy,
)
from app.integrations.transport import HttpxTransport

SYSTEM_LOGIN = "relay-worker"
NATIVE_SOURCE = "devin-automation"
LOGGER = logging.getLogger("relay.native_intake")


@dataclass
class NativeIntakeState:
    """What the worker last saw when polling Relay's Devin automations."""

    automation_id: str | None = None
    fix_automation_id: str | None = None
    last_polled_at: datetime | None = None
    last_error: str | None = None
    ignored_session_ids: set[str] = field(default_factory=set)
    trigger_issue_numbers: dict[str, int | None] = field(default_factory=dict)


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


class AutomationRegistry(Protocol):
    """Where the worker records the automation handles it found or created."""

    def save(self, handle: AutomationHandle) -> None: ...


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
    automation_registry: AutomationRegistry | None = None
    native_intake: NativeIntakeState = field(default_factory=NativeIntakeState)

    def automation_handles(self) -> dict[TaskKind, AutomationHandle]:
        """Find-or-create Relay's reproduction and fix automations."""
        if self.automations is None:
            return {}
        now = self.service.clock.now()
        handles = {
            kind: self.automations.ensure_automation(kind, now=now) for kind in AUTOMATION_KINDS
        }
        if self.automation_registry is not None:
            for handle in handles.values():
                self.automation_registry.save(handle)
        return handles

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
        if job.kind in {JobKind.PUBLISH_QUESTIONS, JobKind.PUBLISH_REMINDER}:
            # Devin's own sessions talk to the reporter; Relay never comments.
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
        """Mirror the sessions Devin's own GitHub triggers started.

        Both automations fire inside Devin (``github:issues`` for triage +
        reproduction, ``github:issue_comment`` for the fix), so Relay learns
        about work by listing their sessions. A session is adopted only once
        its structured output names a Superset issue; the issue is then
        re-checked against the deterministic gate and enrolled with the data
        read back from GitHub. Sessions that never identify an issue, or fail
        the gate, are left alone.
        """
        if self.automations is None:
            return
        now = self.service.clock.now()
        state = self.native_intake
        try:
            handles = self.automation_handles()
            rows = [
                (handle, row)
                for handle in handles.values()
                for row in self.automations.list_automation_sessions(handle.automation_id)
            ]
        except Exception as error:
            state.last_error = f"{type(error).__name__}: {error}"
            LOGGER.warning("native intake poll failed: %s", state.last_error)
            return
        state.automation_id = handles[TaskKind.REPRODUCTION].automation_id
        state.fix_automation_id = handles[TaskKind.FIX].automation_id
        state.last_polled_at = now
        state.last_error = None
        with self.uow_factory() as uow:
            known = {s.external_session_id for s in uow.list_sessions() if s.external_session_id}
        for handle, row in sorted(rows, key=lambda pair: pair[1].created_at):
            if row.session_id in known or row.session_id in state.ignored_session_ids:
                continue
            number = native_issue_number(row.structured_output)
            if number is None:
                number = self._trigger_issue_number(state, row)
            if number is None:
                if row.is_terminal:
                    state.ignored_session_ids.add(row.session_id)
                    LOGGER.info(
                        "ignoring automation session %s: it never identified an issue",
                        row.session_id,
                    )
                continue
            try:
                if handle.kind is TaskKind.FIX:
                    adopted = self._adopt_native_fix_session(handle, row, number, now)
                else:
                    adopted = self._adopt_native_session(handle, row, number, now)
            except Exception:
                LOGGER.exception("adopting automation session %s failed", row.session_id)
                continue
            if adopted is True:
                known.add(row.session_id)
            elif adopted is False:
                state.ignored_session_ids.add(row.session_id)
        self._expire_waiting_native_sessions()

    def _trigger_issue_number(
        self, state: NativeIntakeState, row: AutomationSessionRow
    ) -> int | None:
        """Issue named by the GitHub event Devin appended to the automation prompt."""
        if row.session_id not in state.trigger_issue_numbers:
            try:
                _, messages = self.devin.fetch_conversation(row.session_id)
            except Exception as error:
                LOGGER.warning(
                    "reading the trigger of automation session %s failed: %s",
                    row.session_id,
                    error,
                )
                return None
            state.trigger_issue_numbers[row.session_id] = trigger_issue_number(messages)
        return state.trigger_issue_numbers[row.session_id]

    def _native_output(self, issue: Issue, snapshot: SessionSnapshot) -> JsonObject | None:
        """Devin's structured output, or the same JSON it posted as a message instead."""
        if native_issue_number(snapshot.structured_output) is not None:
            return snapshot.structured_output
        _, messages = self.devin.fetch_conversation(snapshot.session_id)
        fallback = native_output_from_conversation(messages, issue_number=issue.external_number)
        return fallback or snapshot.structured_output

    def _native_phase_done(
        self, issue: Issue, session: AgentSession, snapshot: SessionSnapshot
    ) -> bool:
        """A native session that reported ``phase: done`` and then went to sleep
        waiting for a user is finished, not stuck."""
        if not self._is_native(session):
            return False
        try:
            output = self._native_output(issue, snapshot)
        except Exception as error:
            LOGGER.warning("reading output of session %s failed: %s", snapshot.session_id, error)
            return False
        return output is not None and output.get("phase") == "done"

    def _adopt_native_fix_session(
        self,
        handle: AutomationHandle,
        row: AutomationSessionRow,
        number: int,
        now: datetime,
    ) -> bool | None:
        """Bind a fix session Devin started to the fix record the lifecycle queued."""
        with self.uow_factory() as uow:
            repo = uow.get_repository()
            issue = uow.get_issue_by_number(repo.id, number) if repo is not None else None
            if issue is None:
                # Relay has not seen the reproduction yet; look again next poll.
                return None if not row.is_terminal else False
            waiting = self._waiting_native_session(uow, issue, SessionKind.FIX)
            if waiting is None:
                # Devin's fix session often shows up before Relay has mirrored
                # the reproduction that queues the fix record; keep looking
                # while the issue can still get there.
                if issue.state not in TERMINAL_STATES:
                    return None
                LOGGER.info(
                    "issue #%s is %s; not adopting fix session %s",
                    number,
                    issue.state.value,
                    row.session_id,
                )
                return False
            self._bind_native_row(waiting, handle, row, now)
            uow.save_session(waiting)
            uow.add_session_event(
                SessionEvent(
                    session_id=waiting.id,
                    label="Devin session adopted",
                    detail=(
                        f"Started by automation {handle.automation_id} on"
                        f" {GITHUB_ISSUE_COMMENT_EVENT_TYPE} for #{number}"
                    ),
                    created_at=now,
                )
            )
        return True

    def _expire_waiting_native_sessions(self) -> None:
        with self.uow_factory() as uow:
            waiting = [
                s
                for s in uow.list_sessions()
                if self._is_native(s)
                and s.kind in (SessionKind.REPRODUCTION, SessionKind.FIX)
                and s.external_session_id is None
                and s.state is SessionState.RUNNING
            ]
        for session in waiting:
            self._expire_waiting(session)

    def _adopt_native_session(
        self,
        handle: AutomationHandle,
        row: AutomationSessionRow,
        number: int,
        now: datetime,
    ) -> bool | None:
        """``True`` once linked, ``False`` to ignore for good, ``None`` to retry later."""
        snapshot = self.github.get_issue(number)
        # Intake is every opened issue; a trusted label only gates the legacy webhook path.
        decision = evaluate_issue_event(
            replace(self.gate_policy, trusted_label=None),
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
                waiting = self._waiting_native_session(uow, issue, SessionKind.REPRODUCTION)
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
            if issue.state not in (IssueState.TRIAGE, IssueState.AWAITING_REPORTER):
                LOGGER.info(
                    "issue #%s is %s; not adopting automation session %s",
                    number,
                    issue.state.value,
                    row.session_id,
                )
                return False
            if any(
                s.kind == SessionKind.TRIAGE
                and s.issue_revision == issue.revision
                and s.state is SessionState.RUNNING
                for s in uow.list_sessions(issue_id=issue.id)
            ):
                # A triage session is underway; the reproduction it may create
                # will wait for this session, so look again next poll.
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

    def _begin_follow_up(self, uow: UnitOfWork, issue: Issue, session: AgentSession) -> None:
        """A reporter comment re-triggered triage: retire the open questions first."""
        self.service.apply(
            uow,
            Event(
                issue_id=issue.id,
                source="devin",
                delivery_id=f"session:{session.id}:follow_up",
                type=EventType.REPORTER_COMMENT,
                actor=self._actor(ActorRole.SYSTEM),
                payload={"follow_up": True, "session_id": str(session.id)},
                correlation_id=session.correlation_id,
            ),
        )

    @staticmethod
    def _bind_native_row(
        session: AgentSession, handle: AutomationHandle, row: AutomationSessionRow, now: datetime
    ) -> None:
        session.automation_id = handle.automation_id
        session.dispatched_at = session.dispatched_at or row.created_at
        session.trigger = LiveWorkerRuntime._native_trigger(handle.kind)
        session.external_session_id = row.session_id
        session.external_session_url = row.url
        session.current_action = "Devin session running"
        session.progress_source = NATIVE_SOURCE
        session.progress_synced_at = now
        session.started_at = session.started_at or row.created_at
        session.last_heartbeat_at = row.updated_at
        session.updated_at = now

    @staticmethod
    def _native_trigger(kind: TaskKind) -> str:
        return (
            GITHUB_ISSUES_EVENT_TYPE
            if kind is TaskKind.REPRODUCTION
            else GITHUB_ISSUE_COMMENT_EVENT_TYPE
        )

    @staticmethod
    def _waiting_native_session(
        uow: UnitOfWork, issue: Issue, kind: SessionKind
    ) -> AgentSession | None:
        return next(
            (
                s
                for s in uow.list_sessions(issue_id=issue.id)
                if s.kind is kind
                and s.trigger in NATIVE_EVENT_TYPES
                and s.external_session_id is None
                and s.state is SessionState.RUNNING
            ),
            None,
        )

    @staticmethod
    def _is_native(session: AgentSession) -> bool:
        return session.trigger in NATIVE_EVENT_TYPES and session.automation_id is not None

    def _expire_waiting(self, pending: AgentSession) -> None:
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
                "Devin's automation did not start a session within the budget",
            )

    def _start_classification(self, uow: UnitOfWork, issue: Issue, job: Job) -> None:
        """Nothing to launch: Devin's ``github:issues`` trigger already started the
        triage session, and adoption records it against this issue."""
        return

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
        if task.kind not in AUTOMATION_KINDS or self.automations is None:
            raise RuntimeError(f"{task.kind.value} sessions are started only by Devin Automations")
        handle = self.automations.ensure_automation(task.kind, now=self.service.clock.now())
        self._await_native_session(uow, issue, session, handle)

    def _await_native_session(
        self, uow: UnitOfWork, issue: Issue, session: AgentSession, handle: AutomationHandle
    ) -> None:
        """Park a lifecycle-created record until Devin's own session for the issue shows up.

        Relay never starts sessions; the one Devin's trigger creates for the same
        issue is bound to this record by issue number during adoption.
        """
        now = self.service.clock.now()
        trigger = self._native_trigger(handle.kind)
        session.automation_id = handle.automation_id
        session.dispatched_at = now
        session.trigger = trigger
        session.state = SessionState.RUNNING
        session.current_action = (
            f"Waiting for Devin's {trigger} automation session for #{issue.external_number}"
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
                if snapshot.status == SessionStatus.COMPLETED or (
                    snapshot.status == SessionStatus.NEEDS_ATTENTION
                    and self._native_phase_done(issue, session, snapshot)
                ):
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
        if not self._is_native(session):
            raise RuntimeError("only Devin Automation sessions are tracked")
        self._complete_native_session(uow, issue, session, self._task(uow, issue, session))

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
        structured_output = self._native_output(issue, snapshot)
        if session.kind is SessionKind.FIX:
            self._apply_native_fix(uow, issue, session, structured_output)
            return
        output = parse_native_triage_output(structured_output)
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
            if issue.state is IssueState.AWAITING_REPORTER:
                # A comment that was not a reporter follow-up; nothing changes.
                self._finish_native_triage(uow, session, output)
                return
            raise RuntimeError(f"Devin session {external_id} ended without a classification")
        if issue.state is IssueState.AWAITING_REPORTER:
            self._begin_follow_up(uow, issue, session)
            issue = uow.get_issue(issue.id) or issue
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
        if refreshed is None:
            return
        if refreshed.state is not IssueState.REPRODUCING:
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

    def _apply_native_fix(
        self,
        uow: UnitOfWork,
        issue: Issue,
        session: AgentSession,
        structured_output: JsonObject | None,
    ) -> None:
        output = parse_native_fix_output(structured_output)
        if output.issue_number != issue.external_number:
            raise RuntimeError(
                f"Devin session {session.external_session_id} reported issue"
                f" #{output.issue_number}, expected #{issue.external_number}"
            )
        pull_request = output.pull_request
        if pull_request is None:
            self._fail_session(
                uow,
                issue,
                session,
                output.blocked_reason or "Devin session ended without a pull request",
            )
            return
        head = self.github.get_pull_request_head(pull_request.number)
        self._apply_session(
            uow,
            session,
            EventType.FIX_RESULT,
            {
                "session_id": str(session.id),
                "summary": output.summary,
                "pull_request": {
                    "repository": SUPERSET_REPOSITORY.full_name,
                    "number": pull_request.number,
                    "head_branch": pull_request.head_branch,
                    "head_sha": head.sha,
                    "url": pull_request.url,
                },
            },
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
                f"Implement the minimal fix for reproduced issue #{issue.external_number}, "
                f"add regression coverage, and open a pull request whose body starts with "
                f"`Fixes #{issue.external_number}`."
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
    automation_registry: AutomationRegistry | None = None,
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
    automations = LiveDevinAutomationClient(
        transport=transport,
        token_provider=StaticTokenProvider(devin_token),
        org_id=org_id,
        sessions=devin,
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
        automation_registry=automation_registry,
    )
