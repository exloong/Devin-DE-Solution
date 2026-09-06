from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, Header, Request, status

from app.api.deps import Ctx
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import Actor, Event, Issue
from app.domain.states import ActorRole, EventType
from app.integrations.github_webhooks import GitHubWebhookEnvelope, GitHubWebhookVerifier
from app.persistence import runtime_status

router = APIRouter()
MAX_WEBHOOK_BYTES = 1_048_576


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(container: Mapping[str, object], key: str, default: str = "") -> str:
    value = container.get(key)
    return value if isinstance(value, str) else default


def _issue_by_number(ctx: Ctx, number: int) -> Issue | None:
    with ctx.uow_factory() as uow:
        repository = uow.get_repository()
        return None if repository is None else uow.get_issue_by_number(repository.id, number)


def _seen_delivery(ctx: Ctx, delivery_id: str) -> bool:
    with ctx.uow_factory() as uow:
        return uow.get_event_by_delivery("github", delivery_id) is not None


def _issue_by_pr(ctx: Ctx, number: int) -> tuple[Issue, str] | None:
    with ctx.uow_factory() as uow:
        for issue in uow.list_issues():
            for pr in uow.list_pull_requests(issue.id):
                if pr.number == number:
                    return issue, pr.head_sha
    return None


async def _read_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BYTES:
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                "GitHub webhook body exceeds the size limit",
                {"max_bytes": MAX_WEBHOOK_BYTES},
            )
    return bytes(body)


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    ctx: Ctx,
    x_github_delivery: str = Header(alias="X-GitHub-Delivery", min_length=1, max_length=200),
    x_github_event: str = Header(alias="X-GitHub-Event", min_length=1, max_length=60),
    x_hub_signature_256: str = Header(alias="X-Hub-Signature-256", min_length=1, max_length=200),
) -> dict[str, object]:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
    secret_file = os.environ.get("GITHUB_WEBHOOK_SECRET_FILE", "").strip()
    if secret and secret_file:
        raise DomainError(
            ErrorCode.INTERNAL,
            "GitHub webhook ingress is misconfigured",
            {"hint": "configure only one webhook secret source"},
        )
    if secret_file:
        try:
            secret = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise DomainError(
                ErrorCode.INTERNAL,
                "GitHub webhook secret file cannot be read",
            ) from error
    if not secret:
        raise DomainError(
            ErrorCode.INTERNAL,
            "GitHub webhook ingress is disabled",
            {"hint": "configure GITHUB_WEBHOOK_SECRET"},
        )
    raw_body = await _read_body(request)
    accepted = GitHubWebhookVerifier(secret=secret.encode()).accept(
        GitHubWebhookEnvelope.from_request(
            headers={
                "x-github-delivery": x_github_delivery,
                "x-github-event": x_github_event,
                "x-hub-signature-256": x_hub_signature_256,
            },
            raw_body=raw_body,
        )
    )
    with ctx.engine.begin() as conn:
        runtime_status.touch(
            conn,
            runtime_status.GITHUB_WEBHOOK,
            accepted.event_name.value,
            ctx.service.clock.now(),
        )
    payload = accepted.redacted_payload
    issue_data = _mapping(payload.get("issue"))
    pull_request = _mapping(payload.get("pull_request"))
    user = _mapping(payload.get("sender"))
    actor_login = _text(user, "login", "github")
    event_type: EventType | None = None
    issue: Issue | None = None
    event_payload: dict[str, object] = {}
    actor = Actor(role=ActorRole.SYSTEM, login="github-webhook")

    if accepted.event_name.value == "issues":
        number = accepted.issue_number
        if number is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "issue number is required")
        if accepted.action in ("opened", "labeled"):
            # Intake is Devin-native: the reproduction automation's
            # github:issues trigger enrolls issues, never this webhook.
            return {
                "accepted": True,
                "delivery": accepted.delivery.delivery_id,
                "action": "ignored",
                "reason": "intake_is_devin_automation",
            }
        elif accepted.action == "reopened":
            issue = _issue_by_number(ctx, number)
            event_type = EventType.ISSUE_REOPENED
            event_payload = {"body": _text(issue_data, "body")}
    elif accepted.event_name.value == "issue_comment" and accepted.action == "created":
        number = accepted.issue_number
        issue = None if number is None else _issue_by_number(ctx, number)
        if issue is not None and actor_login == issue.reporter_login:
            with ctx.uow_factory() as uow:
                questions = [
                    question
                    for question in uow.list_questions(issue.id, issue.revision)
                    if question.status.value == "open"
                ]
            if questions:
                event_type = EventType.REPORTER_COMMENT
                actor = Actor(role=ActorRole.REPORTER, login=actor_login)
                event_payload = {
                    "answers": [
                        {
                            "field": questions[0].field,
                            "answer": _text(_mapping(payload.get("comment")), "body"),
                        }
                    ]
                }
    elif accepted.event_name.value == "pull_request":
        number = accepted.pull_request_number
        binding = None if number is None else _issue_by_pr(ctx, number)
        if binding is not None:
            issue, stored_head = binding
            head_sha = _text(_mapping(pull_request.get("head")), "sha", stored_head)
            if accepted.action == "synchronize":
                event_type = EventType.PR_SYNCHRONIZED
                event_payload = {"pr_number": number, "head_sha": head_sha}
            elif accepted.action == "opened":
                event_type = EventType.PR_OPENED
                event_payload = {"number": number}
            elif accepted.action == "closed" and pull_request.get("merged") is True:
                event_type = EventType.PR_MERGED
                event_payload = {"pr_number": number, "head_sha": head_sha}
    elif accepted.event_name.value == "pull_request_review" and accepted.action == "submitted":
        number = accepted.pull_request_number
        binding = None if number is None else _issue_by_pr(ctx, number)
        review = _mapping(payload.get("review"))
        review_state = _text(review, "state").lower()
        if binding is not None and review_state in {"approved", "changes_requested"}:
            issue, stored_head = binding
            actor = Actor(role=ActorRole.OWNER, login=actor_login)
            event_type = EventType.HUMAN_REVIEW_SUBMITTED
            event_payload = {
                "pr_number": number,
                "head_sha": _text(_mapping(pull_request.get("head")), "sha", stored_head),
                "state": review_state,
                "rationale": _text(review, "body"),
            }

    if event_type is None:
        return {
            "accepted": True,
            "delivery": accepted.delivery.delivery_id,
            "action": "ignored",
        }
    event = Event(
        issue_id=None if issue is None else issue.id,
        source="github",
        delivery_id=accepted.delivery.delivery_id,
        type=event_type,
        actor=actor,
        payload=event_payload,
        correlation_id=f"github:{accepted.delivery.delivery_id}",
    )
    with ctx.uow_factory() as uow:
        result = ctx.service.apply(uow, event)
    if result.issue is None:
        raise DomainError(ErrorCode.INTERNAL, "webhook transition returned no issue")
    return {
        "accepted": True,
        "delivery": accepted.delivery.delivery_id,
        "issue_id": str(result.issue.id),
        "version": result.issue.version,
        "state": result.issue.state.value,
    }
