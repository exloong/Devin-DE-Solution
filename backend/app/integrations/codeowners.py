"""CODEOWNERS parsing and deterministic, explainable reviewer routing."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

from .errors import ContractValidationError, ValidationCode

CODEOWNERS_PATH = ".github/CODEOWNERS"

_SUPPORTED_PATTERN_CHARACTERS = re.compile(r"^[A-Za-z0-9._*/-]+$")
_SEGMENT_WILDCARD = "[^/]*"

_USER_PATTERN = re.compile(r"^@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$")
_TEAM_PATTERN = re.compile(
    r"^@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9](?:[A-Za-z0-9._-]{0,99}))$"
)
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OwnerKind(str, Enum):
    USER = "user"
    TEAM = "team"
    EMAIL = "email"


@dataclass(frozen=True, order=True)
class Owner:
    """One CODEOWNERS owner reference."""

    kind: OwnerKind
    handle: str

    @classmethod
    def parse(cls, token: str) -> Owner:
        team_match = _TEAM_PATTERN.fullmatch(token)
        if team_match is not None:
            return cls(kind=OwnerKind.TEAM, handle=token[1:])
        user_match = _USER_PATTERN.fullmatch(token)
        if user_match is not None:
            return cls(kind=OwnerKind.USER, handle=user_match.group(1))
        if _EMAIL_PATTERN.fullmatch(token):
            return cls(kind=OwnerKind.EMAIL, handle=token)
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"CODEOWNERS owner {token!r} is malformed",
        )

    @property
    def team_slug(self) -> str:
        if self.kind is not OwnerKind.TEAM:
            raise ValueError("only team owners have a team slug")
        return self.handle.split("/", maxsplit=1)[1]

    def __str__(self) -> str:
        return self.handle if self.kind is OwnerKind.EMAIL else f"@{self.handle}"


@dataclass(frozen=True)
class CodeownersRule:
    """One CODEOWNERS line: a path pattern and its owners."""

    pattern: str
    owners: tuple[Owner, ...]
    line_number: int

    def matches(self, path: str) -> bool:
        return _pattern_matches(self.pattern, path)


def _normalize_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE, "changed path must be non-empty text"
        )
    normalized = path.strip().removeprefix("./").removeprefix("/")
    if ".." in normalized.split("/"):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE, "changed path may not traverse upward"
        )
    return normalized


def _segment_regex(segment: str) -> str:
    """Translate one pattern segment, where ``*`` never crosses ``/``."""
    parts: list[str] = []
    for character in segment:
        parts.append(_SEGMENT_WILDCARD if character == "*" else re.escape(character))
    return "".join(parts)


@lru_cache(maxsize=512)
def compile_codeowners_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a CODEOWNERS pattern using gitignore segment semantics.

    Only the syntax GitHub applies to Superset's file is supported: anchored
    and unanchored paths, directory patterns (``/dir/``), single-segment
    wildcards, and ``**`` as a whole segment. Anything else - character
    classes, ``?``, negation, escapes - fails closed rather than being matched
    by looser Python glob rules that let ``*`` cross ``/``.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE, "CODEOWNERS pattern is empty"
        )
    if pattern != pattern.strip() or not _SUPPORTED_PATTERN_CHARACTERS.fullmatch(
        pattern
    ):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"CODEOWNERS pattern {pattern!r} uses unsupported syntax",
        )
    directory_only = pattern.endswith("/")
    core = pattern.rstrip("/")
    anchored = pattern.startswith("/") or "/" in core.strip("/")
    segments = [segment for segment in core.strip("/").split("/") if segment]
    if not segments:
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"CODEOWNERS pattern {pattern!r} has no path segments",
        )
    for segment in segments:
        if segment == "..":
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                f"CODEOWNERS pattern {pattern!r} may not traverse upward",
            )
        if "**" in segment and segment != "**":
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                f"CODEOWNERS pattern {pattern!r} uses ** inside a segment",
            )

    body = ""
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            body += ".*" if last else "(?:[^/]+/)*"
            continue
        body += _segment_regex(segment) + ("" if last else "/")
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.+" if directory_only else "(?:/.+)?"
    return re.compile(f"^{prefix}{body}{suffix}$")


def _pattern_matches(pattern: str, path: str) -> bool:
    """Match a CODEOWNERS pattern against a repository-relative path."""
    return (
        compile_codeowners_pattern(pattern).fullmatch(_normalize_path(path)) is not None
    )


@dataclass(frozen=True)
class CodeownersFile:
    """Parsed CODEOWNERS content with GitHub last-match-wins semantics."""

    rules: tuple[CodeownersRule, ...]

    @classmethod
    def parse(cls, content: str) -> CodeownersFile:
        if not isinstance(content, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "CODEOWNERS content must be text"
            )
        rules: list[CodeownersRule] = []
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.split("#", maxsplit=1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            # A pattern without owners clears ownership for matching paths,
            # which routing then reports as unowned rather than guessing.
            pattern, owner_tokens = tokens[0], tokens[1:]
            compile_codeowners_pattern(pattern)
            owners = tuple(Owner.parse(token) for token in owner_tokens)
            rules.append(
                CodeownersRule(pattern=pattern, owners=owners, line_number=line_number)
            )
        return cls(rules=tuple(rules))

    def rule_for_path(self, path: str) -> CodeownersRule | None:
        matched: CodeownersRule | None = None
        for rule in self.rules:
            if rule.matches(path):
                matched = rule
        return matched

    def owners_for_path(self, path: str) -> tuple[Owner, ...]:
        rule = self.rule_for_path(path)
        return () if rule is None else rule.owners


class RoutingStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NO_OWNER = "no_owner"


@dataclass(frozen=True)
class ReviewerCandidate:
    """One reviewer candidate and the rule that selected it."""

    owner: Owner
    rule_pattern: str
    rule_line_number: int
    matched_paths: tuple[str, ...]

    @property
    def rationale(self) -> str:
        paths = ", ".join(self.matched_paths)
        return (
            f"{self.owner} owns {self.rule_pattern} "
            f"({CODEOWNERS_PATH}:{self.rule_line_number}) matching {paths}"
        )


@dataclass(frozen=True)
class ReviewerRoutingPolicy:
    """Deterministic routing limits; ambiguity is reported, never guessed."""

    max_reviewers: int = 3
    max_owner_groups: int = 2
    excluded_logins: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_reviewers < 1:
            raise ValueError("max_reviewers must be positive")
        if self.max_owner_groups < 1:
            raise ValueError("max_owner_groups must be positive")


@dataclass(frozen=True)
class ReviewerRoutingDecision:
    """The routing outcome, its candidates, and its explanation."""

    status: RoutingStatus
    candidates: tuple[ReviewerCandidate, ...]
    unowned_paths: tuple[str, ...]
    reason: str
    owner_groups: tuple[tuple[Owner, ...], ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.status is RoutingStatus.RESOLVED

    @property
    def requested_reviewers(self) -> tuple[str, ...]:
        if not self.is_resolved:
            return ()
        return tuple(
            candidate.owner.handle
            for candidate in self.candidates
            if candidate.owner.kind is OwnerKind.USER
        )

    @property
    def requested_team_reviewers(self) -> tuple[str, ...]:
        if not self.is_resolved:
            return ()
        return tuple(
            candidate.owner.team_slug
            for candidate in self.candidates
            if candidate.owner.kind is OwnerKind.TEAM
        )

    @property
    def rationale(self) -> tuple[str, ...]:
        return tuple(candidate.rationale for candidate in self.candidates)


@dataclass
class ReviewerRouter:
    """Derives reviewer candidates from CODEOWNERS and changed paths."""

    codeowners: CodeownersFile
    policy: ReviewerRoutingPolicy = field(default_factory=ReviewerRoutingPolicy)

    def route(self, changed_paths: Sequence[str]) -> ReviewerRoutingDecision:
        if not changed_paths:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "reviewer routing requires at least one changed path",
            )
        normalized = tuple(sorted({_normalize_path(path) for path in changed_paths}))

        matches: dict[Owner, tuple[CodeownersRule, list[str]]] = {}
        owner_groups: set[tuple[Owner, ...]] = set()
        unowned: list[str] = []

        for path in normalized:
            rule = self.codeowners.rule_for_path(path)
            eligible = () if rule is None else self._eligible_owners(rule.owners)
            if rule is None or not eligible:
                unowned.append(path)
                continue
            owner_groups.add(eligible)
            for owner in eligible:
                existing = matches.get(owner)
                if existing is None:
                    matches[owner] = (rule, [path])
                else:
                    existing[1].append(path)

        sorted_groups = tuple(sorted(owner_groups))

        if not matches:
            return ReviewerRoutingDecision(
                status=RoutingStatus.NO_OWNER,
                candidates=(),
                unowned_paths=normalized,
                reason=(
                    f"no eligible owner in {CODEOWNERS_PATH} matches the changed paths"
                ),
            )

        candidates = tuple(
            ReviewerCandidate(
                owner=owner,
                rule_pattern=rule.pattern,
                rule_line_number=rule.line_number,
                matched_paths=tuple(sorted(paths)),
            )
            for owner, (rule, paths) in sorted(
                matches.items(), key=lambda item: (-len(item[1][1]), item[0])
            )
        )

        if len(sorted_groups) > self.policy.max_owner_groups:
            return ReviewerRoutingDecision(
                status=RoutingStatus.AMBIGUOUS,
                candidates=candidates,
                unowned_paths=tuple(unowned),
                reason=(
                    f"{len(sorted_groups)} distinct owner groups exceed the "
                    f"limit of {self.policy.max_owner_groups}; an owner must decide"
                ),
                owner_groups=sorted_groups,
            )
        if unowned:
            return ReviewerRoutingDecision(
                status=RoutingStatus.AMBIGUOUS,
                candidates=candidates,
                unowned_paths=tuple(unowned),
                reason=(
                    "some changed paths have no owner; an owner must decide who "
                    "reviews them"
                ),
                owner_groups=sorted_groups,
            )

        selected = candidates[: self.policy.max_reviewers]
        return ReviewerRoutingDecision(
            status=RoutingStatus.RESOLVED,
            candidates=selected,
            unowned_paths=(),
            reason=(
                f"selected {len(selected)} owner(s) from {CODEOWNERS_PATH} "
                "by changed-path ownership"
            ),
            owner_groups=sorted_groups,
        )

    def _eligible_owners(self, owners: Iterable[Owner]) -> tuple[Owner, ...]:
        excluded = {login.lower() for login in self.policy.excluded_logins}
        return tuple(
            sorted(
                owner
                for owner in owners
                if owner.kind is not OwnerKind.EMAIL
                and owner.handle.lower() not in excluded
            )
        )


def parse_codeowners(content: str) -> CodeownersFile:
    """Convenience wrapper for :meth:`CodeownersFile.parse`."""
    return CodeownersFile.parse(content)


def routing_summary(decision: ReviewerRoutingDecision) -> Mapping[str, object]:
    """Dashboard-facing summary of one routing decision."""
    return {
        "status": decision.status.value,
        "reason": decision.reason,
        "candidates": [
            {
                "owner": str(candidate.owner),
                "kind": candidate.owner.kind.value,
                "rule": f"{CODEOWNERS_PATH}:{candidate.rule_line_number}",
                "pattern": candidate.rule_pattern,
                "matched_paths": list(candidate.matched_paths),
                "rationale": candidate.rationale,
            }
            for candidate in decision.candidates
        ],
        "unowned_paths": list(decision.unowned_paths),
    }
