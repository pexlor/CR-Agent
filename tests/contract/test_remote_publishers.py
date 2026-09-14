from __future__ import annotations

import gzip

import httpx
import pytest
import respx

from code_review_agent.adapters.publication.github import GitHubPublisher
from code_review_agent.adapters.publication.gitlab import GitLabPublisher
from code_review_agent.domain.publication.models import (
    PublicationError,
    PublicationItem,
    PublicationItemKind,
    PublicationPosition,
    PublicationTarget,
)


class Credentials:
    def get(self, *, provider_id: str, alias: str) -> str | None:
        assert alias == "shared"
        return f"{provider_id}-token"


def _target(platform: str) -> PublicationTarget:
    if platform == "github":
        url = "https://github.com/owner/repo/pull/7"
        repository = "owner/repo"
    else:
        url = "https://gitlab.com/group/repo/-/merge_requests/7"
        repository = "group/repo"
    return PublicationTarget(
        platform=platform,
        source_url=url,
        repository=repository,
        number=7,
        base_sha="a" * 40,
        start_sha="b" * 40,
        head_sha="c" * 40,
    )


def _line(side: str = "RIGHT") -> PublicationItem:
    return PublicationItem(
        publication_key="1" * 64,
        kind=PublicationItemKind.LINE,
        body="review body\n\n<!-- cr-agent:publication:key -->",
        body_digest="2" * 64,
        marker="<!-- cr-agent:publication:key -->",
        finding_id="finding-1",
        position=PublicationPosition(
            path="src/a.py",
            old_path="src/old.py",
            new_path="src/a.py",
            line=12,
            side=side,
        ),
    )


def _summary() -> PublicationItem:
    return PublicationItem(
        publication_key="3" * 64,
        kind=PublicationItemKind.SUMMARY,
        body="summary\n\n<!-- cr-agent:publication:summary -->",
        body_digest="4" * 64,
        marker="<!-- cr-agent:publication:summary -->",
    )


@respx.mock
def test_github_verifies_lists_and_posts_exact_payloads() -> None:
    target = _target("github")
    metadata = respx.get("https://api.github.com/repos/owner/repo/pulls/7").mock(
        return_value=httpx.Response(
            200, json={"state": "open", "merged_at": None, "head": {"sha": "c" * 40}}
        )
    )
    respx.get(
        "https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100"
    ).mock(
        return_value=httpx.Response(
            200, json=[{"id": 8, "html_url": "https://g/c/8", "body": _summary().body}]
        )
    )
    respx.get(
        "https://api.github.com/repos/owner/repo/pulls/7/comments?per_page=100"
    ).mock(return_value=httpx.Response(200, json=[]))
    line_route = respx.post(
        "https://api.github.com/repos/owner/repo/pulls/7/comments"
    ).mock(
        return_value=httpx.Response(201, json={"id": 9, "html_url": "https://g/c/9"})
    )
    summary_route = respx.post(
        "https://api.github.com/repos/owner/repo/issues/7/comments"
    ).mock(
        return_value=httpx.Response(201, json={"id": 10, "html_url": "https://g/c/10"})
    )
    publisher = GitHubPublisher(Credentials())

    state = publisher.verify_target(target)
    found = publisher.find_markers(target, (_summary().marker,))
    line = publisher.create_line_comment(target, _line())
    summary = publisher.create_summary_comment(target, _summary())

    assert metadata.called and state.open and state.head_sha == "c" * 40
    assert found[_summary().marker].remote_id == "8"
    assert line.remote_id == "9" and summary.remote_id == "10"
    assert line_route.calls[0].request.headers["authorization"] == "Bearer github-token"
    assert (
        line_route.calls[0].request.headers["accept"]
        == "application/vnd.github+json"
    )
    assert (
        line_route.calls[0].request.headers["x-github-api-version"] == "2022-11-28"
    )
    assert line_route.calls[0].request.content == (
        b'{"body":"review body\\n\\n<!-- cr-agent:publication:key -->",'
        b'"commit_id":"cccccccccccccccccccccccccccccccccccccccc",'
        b'"line":12,"path":"src/a.py","side":"RIGHT"}'
    )
    assert summary_route.calls[0].request.content == (
        b'{"body":"summary\\n\\n<!-- cr-agent:publication:summary -->"}'
    )


@respx.mock
def test_gitlab_posts_old_line_with_complete_diff_position() -> None:
    target = _target("gitlab")
    route = respx.post(
        "https://gitlab.com/api/v4/projects/group%2Frepo/merge_requests/7/discussions"
    ).mock(
        return_value=httpx.Response(
            201, json={"id": "discussion-1", "web_url": "https://gl/d/1"}
        )
    )
    publisher = GitLabPublisher(Credentials())

    result = publisher.create_line_comment(target, _line("LEFT"))

    assert result.remote_id == "discussion-1"
    assert route.calls[0].request.headers["authorization"] == "Bearer gitlab-token"
    assert route.calls[0].request.content == (
        b'{"body":"review body\\n\\n<!-- cr-agent:publication:key -->",'
        b'"position":{"base_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"head_sha":"cccccccccccccccccccccccccccccccccccccccc",'
        b'"new_path":"src/a.py","old_line":12,"old_path":"src/old.py",'
        b'"position_type":"text",'
        b'"start_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}'
    )


@respx.mock
def test_transport_error_after_post_is_classified_unknown() -> None:
    target = _target("github")
    respx.post("https://api.github.com/repos/owner/repo/issues/7/comments").mock(
        side_effect=httpx.ReadTimeout("lost response")
    )

    with pytest.raises(PublicationError) as raised:
        GitHubPublisher(Credentials()).create_summary_comment(target, _summary())

    assert raised.value.code == "publication_result_unknown"
    assert raised.value.outcome_unknown is True


@respx.mock
def test_marker_listing_must_complete_before_absence_is_trusted() -> None:
    target = _target("gitlab")
    respx.get(
        "https://gitlab.com/api/v4/projects/group%2Frepo/merge_requests/7/notes?per_page=100"
    ).mock(return_value=httpx.Response(500, json={"message": "failure"}))

    with pytest.raises(PublicationError) as raised:
        GitLabPublisher(Credentials()).find_markers(target, (_summary().marker,))

    assert raised.value.code == "publication_provider_unavailable"
    assert raised.value.outcome_unknown is False


@respx.mock
def test_marker_listing_rejects_cross_origin_pagination() -> None:
    target = _target("github")
    respx.get(
        "https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"link": '<https://attacker.example/comments>; rel="next"'},
            json=[],
        )
    )

    with pytest.raises(PublicationError) as raised:
        GitHubPublisher(Credentials()).find_markers(target, (_summary().marker,))

    assert raised.value.code == "publication_provider_response_invalid"


def test_post_response_limit_stops_stream_before_buffering_rest() -> None:
    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 11
            raise AssertionError("response stream was consumed past the byte limit")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, stream=OversizedStream(), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = GitHubPublisher(Credentials(), client=client, max_response_bytes=10)

    with pytest.raises(PublicationError) as raised:
        publisher.create_summary_comment(_target("github"), _summary())

    assert raised.value.code == "publication_result_unknown"
    assert raised.value.outcome_unknown is True


def test_streamed_gzip_response_is_decoded_exactly_once() -> None:
    compressed = gzip.compress(b'[{"id":8,"html_url":"https://g/c/8",'
                               b'"body":"<!-- cr-agent:publication:summary -->"}]')

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = (
            compressed
            if path.endswith("/issues/7/comments")
            else gzip.compress(b"[]")
        )
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=payload,
            request=request,
        )

    publisher = GitHubPublisher(
        Credentials(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    found = publisher.find_markers(
        _target("github"), ("<!-- cr-agent:publication:summary -->",)
    )

    assert found["<!-- cr-agent:publication:summary -->"].remote_id == "8"
