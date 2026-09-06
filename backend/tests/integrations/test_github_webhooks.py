"""Webhook ingress: raw-body signature, repository allowlist, deduplication."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from app.integrations import (
    ContractValidationError,
    DeliveryKey,
    GitHubEventName,
    GitHubWebhookEnvelope,
    GitHubWebhookVerifier,
    InMemoryDeliveryDeduplicator,
    ValidationCode,
    verify_sha256_signature,
)

from conftest import WEBHOOK_SECRET


def sign(raw_body: bytes, secret: bytes = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def issue_body(*, full_name: str = "exloong/superset", number: int = 40) -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": full_name},
            "issue": {
                "number": number,
                "title": "Chart export fails",
                "body": "token: ghp_0123456789abcdefghij",
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def envelope(
    raw_body: bytes,
    *,
    delivery_id: str = "d-1",
    event: str = "issues",
    signature: str | None = None,
) -> GitHubWebhookEnvelope:
    return GitHubWebhookEnvelope.from_request(
        headers={
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": (
                signature if signature is not None else sign(raw_body)
            ),
        },
        raw_body=raw_body,
    )


def test_accepts_signed_superset_issue_event() -> None:
    raw_body = issue_body()
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    event = verifier.accept(envelope(raw_body))

    assert event.event_name is GitHubEventName.ISSUES
    assert event.action == "opened"
    assert event.repository.full_name == "exloong/superset"
    assert event.issue_number == 40
    assert event.delivery == DeliveryKey(source="github", delivery_id="d-1")


def test_accepts_signed_superset_ping_event() -> None:
    raw_body = json.dumps(
        {"repository": {"full_name": "exloong/superset"}, "zen": "Keep it logically awesome."},
        separators=(",", ":"),
    ).encode("utf-8")
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    event = verifier.accept(envelope(raw_body, event="ping"))

    assert event.event_name is GitHubEventName.PING
    assert event.action is None
    assert event.repository.full_name == "exloong/superset"
    assert event.issue_number is None
    assert event.pull_request_number is None


def test_signature_is_computed_over_the_exact_raw_body() -> None:
    raw_body = issue_body()
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)
    # Re-serializing the same JSON with different separators changes the bytes.
    reserialized = json.dumps(json.loads(raw_body)).encode("utf-8")

    assert reserialized != raw_body
    with pytest.raises(ContractValidationError) as error:
        verifier.accept(envelope(reserialized, signature=sign(raw_body)))
    assert error.value.code is ValidationCode.INVALID_SIGNATURE


@pytest.mark.parametrize(
    "signature",
    ["", "sha1=deadbeef", "sha256=notahexdigest", "sha256=" + "0" * 64],
)
def test_rejects_invalid_signature_headers(signature: str) -> None:
    raw_body = issue_body()
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    with pytest.raises(ContractValidationError) as error:
        verifier.accept(envelope(raw_body, signature=signature))
    assert error.value.code is ValidationCode.INVALID_SIGNATURE


def test_rejects_signature_from_another_secret() -> None:
    raw_body = issue_body()
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    with pytest.raises(ContractValidationError):
        verifier.accept(envelope(raw_body, signature=sign(raw_body, b"other")))


def test_rejects_repository_other_than_superset() -> None:
    raw_body = issue_body(full_name="apache/superset")
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    with pytest.raises(ContractValidationError) as error:
        verifier.accept(envelope(raw_body))
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_rejects_unaccepted_event_name() -> None:
    raw_body = issue_body()
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    with pytest.raises(ContractValidationError) as error:
        verifier.accept(envelope(raw_body, event="push"))
    assert error.value.code is ValidationCode.MALFORMED_ENVELOPE


def test_rejects_malformed_json_body() -> None:
    raw_body = b"{not json"
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    with pytest.raises(ContractValidationError) as error:
        verifier.accept(envelope(raw_body))
    assert error.value.code is ValidationCode.MALFORMED_ENVELOPE


def test_rejects_json_array_body() -> None:
    raw_body = b"[]"
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    with pytest.raises(ContractValidationError):
        verifier.accept(envelope(raw_body))


def test_repeated_delivery_id_is_rejected_once_recorded() -> None:
    raw_body = issue_body()
    deduplicator = InMemoryDeliveryDeduplicator()
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET, deduplicator=deduplicator)

    verifier.accept(envelope(raw_body))
    with pytest.raises(ContractValidationError) as error:
        verifier.accept(envelope(raw_body))

    assert error.value.code is ValidationCode.DUPLICATE_DELIVERY
    assert len(deduplicator) == 1


def test_distinct_delivery_ids_are_both_accepted() -> None:
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    first = verifier.accept(envelope(issue_body(), delivery_id="d-1"))
    second = verifier.accept(envelope(issue_body(number=41), delivery_id="d-2"))

    assert first.delivery != second.delivery


def test_rejected_delivery_is_not_recorded_and_may_be_retried() -> None:
    raw_body = issue_body()
    deduplicator = InMemoryDeliveryDeduplicator()
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET, deduplicator=deduplicator)

    with pytest.raises(ContractValidationError):
        verifier.accept(envelope(raw_body, signature="sha256=" + "1" * 64))
    assert len(deduplicator) == 0

    assert verifier.accept(envelope(raw_body)).issue_number == 40


def test_malformed_delivery_id_is_rejected_at_envelope_construction() -> None:
    with pytest.raises(ContractValidationError) as error:
        DeliveryKey(source="github", delivery_id="")
    assert error.value.code is ValidationCode.MALFORMED_ENVELOPE


def test_delivery_key_rejects_a_foreign_source() -> None:
    with pytest.raises(ContractValidationError):
        DeliveryKey(source="gitlab", delivery_id="d-1")


def test_reporter_supplied_secrets_are_redacted_in_the_retained_payload() -> None:
    verifier = GitHubWebhookVerifier(secret=WEBHOOK_SECRET)

    event = verifier.accept(envelope(issue_body()))

    assert "ghp_0123456789abcdefghij" not in json.dumps(dict(event.redacted_payload))


def test_verify_signature_helper_requires_bytes() -> None:
    with pytest.raises(TypeError):
        verify_sha256_signature(
            secret=WEBHOOK_SECRET,
            raw_body="not bytes",  # type: ignore[arg-type]
            signature_header=None,
        )
