"""Table definitions.

Each table keeps the columns needed for uniqueness, lookups, and locking, plus a
``data`` JSON document holding the full typed record. That keeps the schema
portable between SQLite and PostgreSQL and lets the domain models evolve
without a migration for every optional field.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()


def _id() -> Column[str]:
    return Column("id", String(36), primary_key=True)


repositories = Table(
    "repositories",
    metadata,
    _id(),
    Column("full_name", String(200), nullable=False, unique=True),
    Column("data", JSON, nullable=False),
)

issues = Table(
    "issues",
    metadata,
    _id(),
    Column("repository_id", String(36), nullable=False, index=True),
    Column("external_number", Integer, nullable=False),
    Column("state", String(40), nullable=False, index=True),
    Column("owner_team", String(120), nullable=True, index=True),
    Column("version", Integer, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
    UniqueConstraint("repository_id", "external_number", name="uq_issue_repo_number"),
)

issue_revisions = Table(
    "issue_revisions",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=False, index=True),
    Column("revision", Integer, nullable=False),
    Column("data", JSON, nullable=False),
    UniqueConstraint("issue_id", "revision", name="uq_issue_revision"),
)

events = Table(
    "events",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=True, index=True),
    Column("source", String(60), nullable=False),
    Column("delivery_id", String(200), nullable=False),
    Column("type", String(60), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
    UniqueConstraint("source", "delivery_id", name="uq_event_delivery"),
)

transition_attempts = Table(
    "transition_attempts",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=False, index=True),
    Column("event_id", String(36), nullable=False, unique=True),
    Column("status", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

information_requests = Table(
    "information_requests",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=False, index=True),
    Column("issue_revision", Integer, nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

artifacts = Table(
    "artifacts",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=False, index=True),
    Column("session_id", String(36), nullable=True, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

human_decisions = Table(
    "human_decisions",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

jobs = Table(
    "jobs",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=True, index=True),
    Column("kind", String(60), nullable=False),
    Column("idempotency_key", String(300), nullable=False, unique=True),
    Column("status", String(20), nullable=False, index=True),
    Column("run_after", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

agent_sessions = Table(
    "agent_sessions",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=False, index=True),
    Column("state", String(30), nullable=False, index=True),
    Column("workspace_released", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

session_events = Table(
    "session_events",
    metadata,
    _id(),
    Column("session_id", String(36), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

conversation_messages = Table(
    "conversation_messages",
    metadata,
    _id(),
    Column("session_id", String(36), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

session_outputs = Table(
    "session_outputs",
    metadata,
    _id(),
    Column("session_id", String(36), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

pull_requests = Table(
    "pull_requests",
    metadata,
    _id(),
    Column("issue_id", String(36), nullable=False, index=True),
    Column("number", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)

audit_entries = Table(
    "audit_entries",
    metadata,
    _id(),
    Column("correlation_id", String(60), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("data", JSON, nullable=False),
)
