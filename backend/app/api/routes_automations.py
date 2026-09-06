"""Operator management of Devin Automations.

Relay does not keep its own automation definitions: every read and write here
is proxied to the Devin v3 Automations API with the server-side token, and
the response is reduced to :class:`AutomationOut` so the browser never sees
inbox URLs, inbox secrets, or the provider token.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.api import queries
from app.api.deps import Ctx, Manager, Reader
from app.api.schemas import (
    AutomationCreate,
    AutomationDetail,
    AutomationOut,
    AutomationPage,
    AutomationSessions,
    AutomationUpdate,
    ProviderSessionOut,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.states import SessionKind
from app.integrations.devin_automations import (
    AutomationPatch,
    AutomationSpec,
    AutomationSummary,
    DevinAutomationClient,
)
from app.integrations.errors import ContractValidationError

router = APIRouter(prefix="/automations")


def _client(ctx: Ctx) -> DevinAutomationClient:
    if ctx.automations is None:
        raise DomainError(
            ErrorCode.PROHIBITED_ACTION,
            "Devin automations are not configured on this deployment",
            {"hint": "set DEVIN_API_TOKEN and DEVIN_ORG_ID, or RELAY_MODE=demo"},
        )
    return ctx.automations


def automation_out(summary: AutomationSummary) -> AutomationOut:
    kind = summary.relay_kind
    return AutomationOut(
        automation_id=summary.automation_id,
        name=summary.name,
        enabled=summary.enabled,
        event_types=list(summary.event_types),
        prompt=summary.prompt,
        metadata=dict(summary.metadata),
        relay_kind=SessionKind(kind.value) if kind is not None else None,
        managed_by_relay=kind is not None,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        created_by=summary.created_by,
        last_invocation_status=summary.last_invocation_status,
        last_invocation_at=summary.last_invocation_at,
        has_inbox=summary.has_inbox,
    )


def _sessions(ctx: Ctx, client: DevinAutomationClient, automation_id: str) -> AutomationSessions:
    with ctx.uow_factory() as uow:
        relay_sessions = queries.list_sessions(
            uow,
            issue_id=None,
            statuses=[],
            dry_run=ctx.scope.dry_run,
            automation_id=automation_id,
        )
    provider_error: str | None = None
    provider_sessions: list[ProviderSessionOut] = []
    try:
        provider_sessions = [
            ProviderSessionOut(
                session_id=row.session_id,
                title=row.title,
                status=row.status,
                url=row.url,
                created_at=row.created_at,
                updated_at=row.updated_at,
                tags=list(row.tags),
            )
            for row in client.list_automation_sessions(automation_id)
        ]
    except ContractValidationError as error:
        provider_error = str(error)
    return AutomationSessions(
        automation_id=automation_id,
        relay_sessions=relay_sessions,
        provider_sessions=provider_sessions,
        provider_error=provider_error,
        generated_at=ctx.service.clock.now(),
    )


@router.get("", response_model=AutomationPage)
def list_automations(ctx: Ctx, _reader: Reader) -> AutomationPage:
    items = [automation_out(s) for s in _client(ctx).list_automations()]
    return AutomationPage(items=items, total=len(items), generated_at=ctx.service.clock.now())


@router.post("", response_model=AutomationOut, status_code=201)
def create_automation(body: AutomationCreate, ctx: Ctx, _manager: Manager) -> AutomationOut:
    created = _client(ctx).create_automation(
        AutomationSpec(
            name=body.name,
            prompt=body.prompt,
            enabled=body.enabled,
            event_type=body.event_type,
            metadata=dict(body.metadata),
        )
    )
    return automation_out(created)


@router.get("/{automation_id}", response_model=AutomationDetail)
def get_automation(automation_id: str, ctx: Ctx, _reader: Reader) -> AutomationDetail:
    client = _client(ctx)
    summary = client.get_automation(automation_id)
    return AutomationDetail(
        automation=automation_out(summary),
        sessions=_sessions(ctx, client, automation_id),
    )


@router.get("/{automation_id}/sessions", response_model=AutomationSessions)
def automation_sessions(automation_id: str, ctx: Ctx, _reader: Reader) -> AutomationSessions:
    return _sessions(ctx, _client(ctx), automation_id)


@router.patch("/{automation_id}", response_model=AutomationOut)
def update_automation(
    automation_id: str, body: AutomationUpdate, ctx: Ctx, _manager: Manager
) -> AutomationOut:
    updated = _client(ctx).update_automation(
        automation_id,
        AutomationPatch(
            name=body.name,
            prompt=body.prompt,
            enabled=body.enabled,
        ),
    )
    return automation_out(updated)


@router.delete("/{automation_id}", status_code=204, response_class=Response)
def delete_automation(automation_id: str, ctx: Ctx, _manager: Manager) -> Response:
    _client(ctx).delete_automation(automation_id)
    return Response(status_code=204)
