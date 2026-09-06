from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from backend.app.integrations.errors import ContractValidationError, ValidationCode
from backend.app.integrations.github import (
    AddLabels,
    CreateBranch,
    CreateDraftPullRequest,
    DeliveryIdentity,
    FakeGitHubAdapter,
    GitHubCapability,
    GitHubWebhookEnvelope,
    InMemoryDeliveryDeduplicator,
    PostIssueComment,
    RepositoryAllowlist,
    RepositoryIdentity,
    verify_sha256_signature,
)


class SignatureValidationTests(unittest.TestCase):
    secret = b"test-webhook-secret"
    body = b'{"repository":{"full_name":"exloong/Devin-DE-Solution"}}'

    def signature(self, body: bytes | None = None) -> str:
        digest = hmac.new(
            self.secret,
            self.body if body is None else body,
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"

    def test_accepts_valid_sha256_signature(self) -> None:
        self.assertTrue(
            verify_sha256_signature(
                secret=self.secret,
                raw_body=self.body,
                signature_header=self.signature(),
            )
        )

    def test_rejects_missing_malformed_and_incorrect_signatures(self) -> None:
        for signature in (
            None,
            "",
            "sha1=bad",
            "sha256=not-hex",
            "sha256=" + ("0" * 63),
            "sha256=" + ("0" * 64),
        ):
            with self.subTest(signature=signature):
                self.assertFalse(
                    verify_sha256_signature(
                        secret=self.secret,
                        raw_body=self.body,
                        signature_header=signature,
                    )
                )

    def test_signature_is_checked_before_json_parsing(self) -> None:
        envelope = GitHubWebhookEnvelope.from_headers(
            headers={
                "X-GitHub-Delivery": "delivery-1",
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=" + ("0" * 64),
            },
            raw_body=b"not-json",
        )

        with self.assertRaises(ContractValidationError) as raised:
            envelope.verified_payload(self.secret)

        self.assertEqual(raised.exception.code, ValidationCode.INVALID_SIGNATURE)


class WebhookAuthorizationTests(unittest.TestCase):
    secret = b"test-webhook-secret"

    def envelope(self, full_name: str) -> GitHubWebhookEnvelope:
        body = json.dumps({"repository": {"full_name": full_name}}).encode()
        signature = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        return GitHubWebhookEnvelope.from_headers(
            headers={
                "x-github-delivery": "delivery-authorization",
                "x-github-event": "issues",
                "x-hub-signature-256": f"sha256={signature}",
            },
            raw_body=body,
        )

    def test_allows_configured_repository_case_insensitively(self) -> None:
        allowlist = RepositoryAllowlist(
            {RepositoryIdentity("ExLoong", "Devin-DE-Solution")}
        )

        repository = self.envelope(
            "exloong/devin-de-solution"
        ).verified_repository(secret=self.secret, allowlist=allowlist)

        self.assertEqual(repository.full_name, "exloong/devin-de-solution")

    def test_rejects_unauthorized_repository(self) -> None:
        allowlist = RepositoryAllowlist(
            {RepositoryIdentity("exloong", "devin-de-solution")}
        )

        with self.assertRaises(ContractValidationError) as raised:
            self.envelope("other/repository").verified_repository(
                secret=self.secret,
                allowlist=allowlist,
            )

        self.assertEqual(
            raised.exception.code,
            ValidationCode.UNAUTHORIZED_REPOSITORY,
        )


class DeliveryDeduplicationTests(unittest.TestCase):
    def test_duplicate_delivery_is_recorded_only_once_concurrently(self) -> None:
        deduplicator = InMemoryDeliveryDeduplicator()
        delivery = DeliveryIdentity(source="github", delivery_id="delivery-concurrent")

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(
                executor.map(lambda _: deduplicator.record_once(delivery), range(100))
            )

        self.assertEqual(sum(results), 1)
        self.assertEqual(len(deduplicator), 1)

    def test_require_new_raises_stable_duplicate_code(self) -> None:
        deduplicator = InMemoryDeliveryDeduplicator()
        delivery = DeliveryIdentity(source="github", delivery_id="delivery-duplicate")
        deduplicator.require_new(delivery)

        with self.assertRaises(ContractValidationError) as raised:
            deduplicator.require_new(delivery)

        self.assertEqual(raised.exception.code, ValidationCode.DUPLICATE_DELIVERY)


class FakeGitHubAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = RepositoryIdentity("exloong", "Devin-DE-Solution")

    def test_records_only_explicit_dry_run_commands(self) -> None:
        commands = (
            PostIssueComment(self.repository, 42, "Please provide a safe fixture."),
            AddLabels(self.repository, 42, ("relay:triage",)),
            CreateBranch(self.repository, "relay/issue-42", "abcdef1"),
            CreateDraftPullRequest(
                self.repository,
                "relay/issue-42",
                "main",
                "Draft scoped fix",
                "Dry-run pull request body",
            ),
        )
        adapter = FakeGitHubAdapter()

        for command in commands:
            adapter.execute(command)

        self.assertEqual(adapter.commands, commands)

    def test_explicit_empty_capability_allowlist_denies_all_commands(self) -> None:
        adapter = FakeGitHubAdapter(allowed_capabilities=frozenset())

        with self.assertRaises(ContractValidationError) as raised:
            adapter.execute(PostIssueComment(self.repository, 42, "Safe text"))

        self.assertEqual(raised.exception.code, ValidationCode.PROHIBITED_CAPABILITY)
        self.assertEqual(adapter.commands, ())

    def test_rejects_unknown_command_type(self) -> None:
        adapter = FakeGitHubAdapter(frozenset(GitHubCapability))

        with self.assertRaises(ContractValidationError) as raised:
            adapter.execute(object())  # type: ignore[arg-type]

        self.assertEqual(raised.exception.code, ValidationCode.PROHIBITED_COMMAND)

    def test_branch_command_rejects_shell_like_or_invalid_refs(self) -> None:
        for branch_name in (
            "relay/$(touch-pwned)",
            "relay/issue;rm",
            "../outside",
            "relay//issue",
            "relay/issue.lock",
        ):
            with self.subTest(branch_name=branch_name):
                with self.assertRaises(ValueError):
                    CreateBranch(self.repository, branch_name, "abcdef1")


if __name__ == "__main__":
    unittest.main()
