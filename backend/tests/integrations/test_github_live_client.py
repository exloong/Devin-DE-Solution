"""Live GitHub client: paginated reads and validated response URLs."""

from __future__ import annotations

import pytest
from app.integrations import (
    SUPERSET_REPOSITORY,
    CommentPosted,
    ContractValidationError,
    CreatePullRequest,
    HttpResponse,
    LiveGitHubClient,
    PostIssueComment,
    PullRequestOpened,
    RecordedTransport,
    StaticTokenProvider,
    ValidationCode,
)
from app.integrations.github_client import (
    MAX_PULL_REQUEST_FILE_PAGES,
    PULL_REQUEST_FILE_PAGE_SIZE,
)
from app.integrations.json_values import JsonValue

from conftest import TARGET_SHA

TOKEN = StaticTokenProvider("test-token")
FILES_URL = "https://api.github.com/repos/exloong/superset/pulls/101/files"
COMMENTS_URL = "https://api.github.com/repos/exloong/superset/issues/40/comments"
PULLS_URL = "https://api.github.com/repos/exloong/superset/pulls"


def test_branch_head_is_resolved_to_an_immutable_commit() -> None:
    transport = RecordedTransport([HttpResponse(200, {"sha": TARGET_SHA})])

    head = client(transport).get_branch_head("master")

    assert head.sha == TARGET_SHA
    assert transport.requests[0].url.endswith("/commits/master")


def test_pull_request_head_is_bound_to_the_requested_number() -> None:
    transport = RecordedTransport(
        [HttpResponse(200, {"number": 101, "head": {"sha": TARGET_SHA}})]
    )

    head = client(transport).get_pull_request_head(101)

    assert head.sha == TARGET_SHA
    assert transport.requests[0].url.endswith("/pulls/101")


def client(transport: RecordedTransport) -> LiveGitHubClient:
    return LiveGitHubClient(transport=transport, token_provider=TOKEN)


def file_page(paths: list[str]) -> HttpResponse:
    entries: list[JsonValue] = [{"filename": path} for path in paths]
    return HttpResponse(200, entries)


def full_page(prefix: str) -> list[str]:
    return [
        f"{prefix}/file_{index:03d}.py" for index in range(PULL_REQUEST_FILE_PAGE_SIZE)
    ]


def test_changed_files_are_paged_beyond_the_first_hundred() -> None:
    first, second = full_page("superset/charts"), full_page("superset/models")
    transport = RecordedTransport(
        [file_page(first), file_page(second), file_page(["tests/unit_tests/api.py"])]
    )

    paths = client(transport).list_pull_request_files(101)

    assert len(paths) == 2 * PULL_REQUEST_FILE_PAGE_SIZE + 1
    assert paths[:1] == (first[0],)
    assert paths[-1] == "tests/unit_tests/api.py"
    assert [request.url for request in transport.requests] == [FILES_URL] * 3
    assert [request.query for request in transport.requests] == [
        {"per_page": "100", "page": "1"},
        {"per_page": "100", "page": "2"},
        {"per_page": "100", "page": "3"},
    ]


def test_a_short_first_page_ends_pagination() -> None:
    transport = RecordedTransport([file_page(["superset/app.py"])])

    assert client(transport).list_pull_request_files(101) == ("superset/app.py",)
    assert len(transport.requests) == 1


def test_an_empty_final_page_ends_pagination() -> None:
    transport = RecordedTransport([file_page(full_page("superset")), file_page([])])

    paths = client(transport).list_pull_request_files(101)

    assert len(paths) == PULL_REQUEST_FILE_PAGE_SIZE
    assert len(transport.requests) == 2


def test_a_pull_request_larger_than_the_bound_fails_closed() -> None:
    transport = RecordedTransport(
        [
            file_page(full_page(f"dir{page:02d}"))
            for page in range(MAX_PULL_REQUEST_FILE_PAGES)
        ]
    )

    with pytest.raises(ContractValidationError) as error:
        client(transport).list_pull_request_files(101)
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE
    assert len(transport.requests) == MAX_PULL_REQUEST_FILE_PAGES


def test_a_repeated_path_across_pages_fails_closed() -> None:
    page = full_page("superset")
    transport = RecordedTransport([file_page(page), file_page(page)])

    with pytest.raises(ContractValidationError) as error:
        client(transport).list_pull_request_files(101)
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_an_oversized_page_fails_closed() -> None:
    transport = RecordedTransport(
        [file_page(full_page("superset") + ["superset/extra.py"])]
    )

    with pytest.raises(ContractValidationError) as error:
        client(transport).list_pull_request_files(101)
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_a_malformed_file_entry_fails_closed() -> None:
    transport = RecordedTransport([HttpResponse(200, [{"path": "superset/app.py"}])])

    with pytest.raises(ContractValidationError) as error:
        client(transport).list_pull_request_files(101)
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def comment(issue_number: int = 40) -> PostIssueComment:
    return PostIssueComment(
        repository=SUPERSET_REPOSITORY, issue_number=issue_number, body="Relay triage"
    )


def test_a_created_comment_url_must_address_the_created_comment() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                201,
                {
                    "id": 9001,
                    "html_url": (
                        "https://github.com/exloong/superset/issues/40"
                        "#issuecomment-9001"
                    ),
                },
            )
        ]
    )

    posted = client(transport).execute(comment())

    assert isinstance(posted, CommentPosted)
    assert transport.requests[0].url == COMMENTS_URL
    assert posted.html_url.endswith("/issues/40#issuecomment-9001")


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/apache/superset/issues/40#issuecomment-9001",
        "http://github.com/exloong/superset/issues/40#issuecomment-9001",
        "https://github.example.com/exloong/superset/issues/40#issuecomment-9001",
        "https://github.com/exloong/superset/issues/41#issuecomment-9001",
        "https://github.com/exloong/superset/issues/40#issuecomment-9002",
        "https://github.com/exloong/superset/pull/40#issuecomment-9001",
    ],
)
def test_a_comment_url_outside_the_created_comment_fails_closed(html_url: str) -> None:
    transport = RecordedTransport(
        [HttpResponse(201, {"id": 9001, "html_url": html_url})]
    )

    with pytest.raises(ContractValidationError):
        client(transport).execute(comment())


def open_pull_request() -> CreatePullRequest:
    return CreatePullRequest(
        repository=SUPERSET_REPOSITORY,
        head_branch="relay/fix-40",
        base_branch="master",
        title="Fix chart export",
        body="Scoped fix plus regression test.",
        source_issue_number=40,
    )


def test_an_opened_pull_request_url_must_name_the_returned_number() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                201,
                {
                    "number": 202,
                    "html_url": "https://github.com/exloong/superset/pull/202",
                },
            )
        ]
    )

    opened = client(transport).execute(open_pull_request())

    assert isinstance(opened, PullRequestOpened)
    assert transport.requests[0].url == PULLS_URL
    assert opened.html_url == "https://github.com/exloong/superset/pull/202"
    assert opened.pull_request_number == 202


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/exloong/superset/pull/203",
        "https://github.com/apache/superset/pull/202",
        "https://github.com/exloong/superset/issues/202",
        "https://github.com/exloong/superset/pull/202/files",
        "https://github.com/exloong/superset-forks/pull/202",
    ],
)
def test_a_pull_request_url_outside_superset_fails_closed(html_url: str) -> None:
    transport = RecordedTransport(
        [HttpResponse(201, {"number": 202, "html_url": html_url})]
    )

    with pytest.raises(ContractValidationError):
        client(transport).execute(open_pull_request())


def test_codeowners_is_read_at_the_immutable_target_commit() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                {
                    "content": "LyoJQGV4bG9vbmcvY29yZQo=",
                    "encoding": "base64",
                },
            )
        ]
    )

    codeowners = client(transport).read_codeowners(TARGET_SHA)

    assert transport.requests[0].query == {"ref": TARGET_SHA}
    assert codeowners.rules
