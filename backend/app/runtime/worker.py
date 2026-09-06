from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.domain.models import Actor, Event, Job, JobKind, JobStatus, PullRequestState
from app.domain.ports import UnitOfWork
from app.domain.states import ActorRole, EventType
from app.domain.transitions import TransitionService
from app.persistence import runtime_status
from app.persistence.database import make_engine, upgrade
from app.persistence.sqlalchemy_uow import SqlAlchemyUnitOfWorkFactory
from app.runtime.live_worker import LiveWorkerRuntime, live_runtime_from_env

LOGGER = logging.getLogger("relay.worker")
SYSTEM = Actor(role=ActorRole.SYSTEM, login="relay-worker")
AGENT = Actor(role=ActorRole.AGENT, login="devin-dry-run")


@dataclass
class Worker:
    engine: Engine
    service: TransitionService
    uow_factory: SqlAlchemyUnitOfWorkFactory
    instance_id: str
    live_runtime: LiveWorkerRuntime | None = None

    @property
    def provider_mode(self) -> str:
        return "live" if self.live_runtime is not None else "dry_run"

    def heartbeat(self) -> None:
        now = datetime.now(timezone.utc)
        runtime_status.touch_all(
            self.engine,
            {
                runtime_status.WORKER: self.instance_id,
                runtime_status.PROVIDER_GITHUB: self.provider_mode,
                runtime_status.PROVIDER_DEVIN: self.provider_mode,
            },
            now,
        )

    def run_once(self) -> int:
        now = self.service.clock.now()
        with self.uow_factory() as uow:
            pending = [
                job
                for job in uow.list_jobs()
                if job.status == JobStatus.PENDING and job.run_after <= now
            ]
        completed = 0
        for job in pending:
            try:
                with self.uow_factory() as uow:
                    self.service.claim_job(uow, job.id, self.instance_id)
                with self.uow_factory() as uow:
                    with self.service.run_claimed_job(uow, job.id, self.instance_id) as claimed:
                        self.execute(uow, claimed)
                completed += 1
            except Exception:
                LOGGER.exception("job %s failed", job.id)
        if self.live_runtime is not None:
            try:
                self.live_runtime.sync()
            except Exception:
                LOGGER.exception("live runtime synchronization failed")
        self.heartbeat()
        return completed

    def execute(self, uow: UnitOfWork, job: Job) -> None:
        if self.live_runtime is not None:
            self.live_runtime.execute(uow, job)
            return
        if job.issue_id is None:
            return
        issue = uow.get_issue(job.issue_id)
        if issue is None:
            raise RuntimeError("job issue no longer exists")
        if job.kind == JobKind.CLASSIFY:
            self._apply(
                uow,
                job,
                EventType.CLASSIFICATION_RESULT,
                {
                    "category": "Bug",
                    "owner_team": "superset-maintainers",
                    "missing_fields": [
                        {
                            "field": "reproduction_steps",
                            "prompt": "What exact steps reproduce the behavior?",
                            "why_it_matters": "A bounded session needs deterministic inputs.",
                            "safe_example": (
                                "Open Explore, select a dataset, then describe the result."
                            ),
                        }
                    ],
                },
                AGENT,
            )
            return
        if job.kind == JobKind.START_REPRODUCTION:
            session_id = self._payload_uuid(job, "session_id")
            self._apply(
                uow,
                job,
                EventType.SESSION_PROGRESS,
                {
                    "session_id": str(session_id),
                    "progress_percent": 70,
                    "current_action": "Running isolated dry-run reproduction",
                    "next_checkpoint": "Publish evidence packet",
                    "label": "Dry-run reproduction",
                    "message": "Reporter content is treated as inert context.",
                },
                AGENT,
                suffix="progress",
            )
            self._apply(
                uow,
                job,
                EventType.SESSION_PROGRESS,
                {
                    "session_id": str(session_id),
                    "progress_percent": 100,
                    "current_action": "Dry-run reproduction complete",
                    "next_checkpoint": "Evidence packet published",
                    "label": "Dry-run reproduction complete",
                    "message": "Bounded local reproduction finished.",
                },
                AGENT,
                suffix="complete-progress",
            )
            self._apply(
                uow,
                job,
                EventType.REPRODUCTION_RESULT,
                {
                    "session_id": str(session_id),
                    "reproduced": True,
                    "evidence": [
                        {
                            "kind": "reproduction_plan",
                            "title": "Bounded reproduction plan",
                            "summary": "Fake adapter plan for local demonstration.",
                        },
                        {
                            "kind": "test_output",
                            "title": "Isolated test output",
                            "summary": "Target behavior reproduced without network credentials.",
                        },
                    ],
                },
                AGENT,
                suffix="result",
            )
            return
        if job.kind == JobKind.START_FIX:
            session_id = self._payload_uuid(job, "session_id")
            self._apply(
                uow,
                job,
                EventType.FIX_SESSION_STARTED,
                {"session_id": str(session_id)},
                SYSTEM,
                suffix="started",
            )
            self._apply(
                uow,
                job,
                EventType.SESSION_PROGRESS,
                {
                    "session_id": str(session_id),
                    "progress_percent": 70,
                    "current_action": "Preparing isolated dry-run fix",
                    "next_checkpoint": "Open fake pull request",
                    "label": "Dry-run fix",
                    "message": "No external repository write is performed.",
                },
                AGENT,
                suffix="progress",
            )
            number = 1000 + issue.external_number
            head_sha = uuid.uuid5(uuid.NAMESPACE_URL, f"relay:{issue.id}:{session_id}").hex
            self._apply(
                uow,
                job,
                EventType.SESSION_PROGRESS,
                {
                    "session_id": str(session_id),
                    "progress_percent": 100,
                    "current_action": "Dry-run fix complete",
                    "next_checkpoint": "Fake pull request opened",
                    "label": "Dry-run fix complete",
                    "message": "The fake pull request is ready for review.",
                },
                AGENT,
                suffix="complete-progress",
            )
            self._apply(
                uow,
                job,
                EventType.FIX_RESULT,
                {
                    "session_id": str(session_id),
                    "pull_request": {
                        "repository": "exloong/superset",
                        "number": number,
                        "title": f"fix: resolve {issue.key}",
                        "head_branch": f"relay/{issue.key.lower()}",
                        "head_sha": head_sha,
                        "url": f"https://github.com/exloong/superset/pull/{number}",
                    },
                },
                AGENT,
                suffix="result",
            )
            return
        if job.kind == JobKind.TRIGGER_DEVIN_REVIEW:
            pr = self._pull_request(uow, issue.id, job)
            self._apply(
                uow,
                job,
                EventType.DEVIN_REVIEW_COMPLETED,
                {
                    "pr_number": pr.number,
                    "head_sha": pr.head_sha,
                    "verdict": "passed",
                    "findings": 0,
                    "url": f"https://app.devin.ai/review/dry-run/{pr.number}",
                },
                AGENT,
            )
            return
        if job.kind == JobKind.RESOLVE_REVIEWERS:
            pr = self._pull_request(uow, issue.id, job)
            self._apply(
                uow,
                job,
                EventType.REVIEWER_ROUTING_RESOLVED,
                {
                    "pr_number": pr.number,
                    "head_sha": pr.head_sha,
                    "state": "resolved",
                    "candidates": [
                        {
                            "team": "superset-maintainers",
                            "rule": "* @superset-maintainers",
                            "members": ["owner-demo"],
                            "paths": ["superset/**"],
                            "rationale": "Deterministic dry-run CODEOWNERS route.",
                        }
                    ],
                    "unowned_paths": [],
                    "ambiguous_paths": [],
                    "rationale": "Fake adapter routing for local demonstration.",
                },
                SYSTEM,
            )
            return
        if job.kind in {
            JobKind.PUBLISH_QUESTIONS,
            JobKind.REQUEST_REVIEWERS,
            JobKind.REMINDER,
            JobKind.PUBLISH_REMINDER,
            JobKind.INACTIVITY,
            JobKind.ESCALATE_OWNER,
            JobKind.SAFE_ALTERNATIVE_REVIEW,
            JobKind.RECOVERY,
            JobKind.PRIVATE_SECURITY_TASK,
            JobKind.CANCEL_SESSION,
            JobKind.DELIVER_SESSION_MESSAGE,
        }:
            return
        raise RuntimeError(f"unsupported job kind: {job.kind.value}")

    def _apply(
        self,
        uow: UnitOfWork,
        job: Job,
        event_type: EventType,
        payload: dict[str, object],
        actor: Actor,
        suffix: str = "effect",
    ) -> None:
        event = Event(
            issue_id=job.issue_id,
            source="worker",
            delivery_id=f"{job.idempotency_key}:{suffix}",
            type=event_type,
            actor=actor,
            payload=payload,
            correlation_id=job.correlation_id,
        )
        self.service.apply(uow, event)

    @staticmethod
    def _payload_uuid(job: Job, field: str) -> uuid.UUID:
        value = job.payload.get(field)
        if not isinstance(value, str):
            raise RuntimeError(f"job payload.{field} is required")
        return uuid.UUID(value)

    @staticmethod
    def _pull_request(uow: UnitOfWork, issue_id: uuid.UUID, job: Job) -> PullRequestState:
        number = job.payload.get("pr_number")
        head_sha = job.payload.get("head_sha")
        for pr in uow.list_pull_requests(issue_id):
            if pr.number == number and pr.head_sha == head_sha:
                return pr
        raise RuntimeError("job pull request binding is stale")


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    database_url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("RELAY_DATABASE_URL")
        or "sqlite:///./relay.sqlite3"
    )
    engine = make_engine(database_url)
    upgrade(engine)
    mode = os.environ.get("RELAY_MODE", "live").strip().lower()
    if mode not in {"demo", "live"}:
        raise RuntimeError("RELAY_MODE must be demo or live")
    service = TransitionService()
    uow_factory = SqlAlchemyUnitOfWorkFactory(engine)
    live_runtime = live_runtime_from_env(service, uow_factory) if mode == "live" else None
    worker = Worker(
        engine=engine,
        service=service,
        uow_factory=uow_factory,
        instance_id=os.environ.get("WORKER_ID", socket.gethostname()),
        live_runtime=live_runtime,
    )
    interval = max(float(os.environ.get("WORKER_POLL_SECONDS", "1")), 0.1)
    LOGGER.info("worker %s started", worker.instance_id)
    while True:
        worker.run_once()
        time.sleep(interval)


if __name__ == "__main__":
    main()
