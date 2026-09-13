from __future__ import annotations

import httpx
import pytest
import respx

from code_review_agent.adapters.input.github import GitHubInputProvider
from code_review_agent.adapters.input.gitlab import GitLabInputProvider
from code_review_agent.adapters.security.scanner import (
    FixedSecurityScanner,
    load_packaged_security_policy,
)
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.security.service import SecurityService


class Credentials:
    def get(self, *, provider_id: str, alias: str) -> str | None:
        return "token" if alias == "shared" else None


def security() -> SecurityService:
    return SecurityService(
        FixedSecurityScanner(), policy=load_packaged_security_policy()
    )


@respx.mock
def test_github_provider_paginates_files_and_fixes_commit_shas() -> None:
    respx.get("https://api.github.com/repos/acme/app/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "merged_at": None,
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
                "url": "https://api.github.com/repos/acme/app/pulls/7",
            },
        )
    )
    respx.get("https://api.github.com/repos/acme/app/pulls/7/files?per_page=100").mock(
        return_value=httpx.Response(
            200,
            headers={
                "link": (
                    "<https://api.github.com/repos/acme/app/pulls/7/files?page=2>; "
                    'rel="next"'
                )
            },
            json=[{"filename": "app.py", "patch": "@@ -1 +1 @@\n-old\n+new"}],
        )
    )
    respx.get("https://api.github.com/repos/acme/app/pulls/7/files?page=2").mock(
        return_value=httpx.Response(200, json=[])
    )

    acquired = GitHubInputProvider(security(), Credentials()).acquire_url(
        task_id="task-1",
        source_url="https://github.com/acme/app/pull/7",
    )

    assert acquired.identity.base_sha == "a" * 40
    assert acquired.identity.head_sha == "b" * 40
    assert acquired.identity.repository_identity == "acme/app"


def test_remote_providers_reject_unsafe_urls() -> None:
    provider = GitHubInputProvider(security(), Credentials())
    with pytest.raises(StableError, match="invalid_source_url"):
        provider.acquire_url(
            task_id="task-1",
            source_url="https://user:pass@github.com/acme/app/pull/7?x=1",
        )


@respx.mock
def test_github_provider_rejects_cross_origin_pagination_link() -> None:
    respx.get("https://api.github.com/repos/acme/app/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "merged_at": None,
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
                "url": "https://api.github.com/repos/acme/app/pulls/7",
            },
        )
    )
    respx.get("https://api.github.com/repos/acme/app/pulls/7/files?per_page=100").mock(
        return_value=httpx.Response(
            200,
            headers={"link": '<https://attacker.example/files?page=2>; rel="next"'},
            json=[{"filename": "app.py", "patch": "@@ -1 +1 @@\n-old\n+new"}],
        )
    )

    with pytest.raises(StableError, match="provider_response_invalid"):
        GitHubInputProvider(security(), Credentials()).acquire_url(
            task_id="task-1",
            source_url="https://github.com/acme/app/pull/7",
        )


@respx.mock
def test_gitlab_provider_rejects_collapsed_diffs() -> None:
    base = "https://gitlab.com/api/v4/projects/acme%2Fapp/merge_requests/8"
    respx.get(base).mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "opened",
                "diff_refs": {"base_sha": "a" * 40, "head_sha": "b" * 40},
            },
        )
    )
    respx.get(base + "/diffs?per_page=100").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "old_path": "app.py",
                    "new_path": "app.py",
                    "diff": "",
                    "collapsed": True,
                }
            ],
        )
    )

    with pytest.raises(StableError, match="input_incomplete"):
        GitLabInputProvider(security(), Credentials()).acquire_url(
            task_id="task-1",
            source_url="https://gitlab.com/acme/app/merge_requests/8",
        )


@respx.mock
def test_gitlab_provider_parses_the_real_bare_array_diffs_response() -> None:
    # GitLab.com's live /diffs endpoint returns a bare JSON array, not an
    # object with a nested "diffs" list and "overflow" flag. This mirrors the
    # exact shape observed from a real request against gitlab.com.
    base = "https://gitlab.com/api/v4/projects/acme%2Fapp/merge_requests/9"
    respx.get(base).mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "opened",
                "diff_refs": {"base_sha": "a" * 40, "head_sha": "b" * 40},
            },
        )
    )
    respx.get(base + "/diffs?per_page=100").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "old_path": "app.py",
                    "new_path": "app.py",
                    "diff": "@@ -1 +1 @@\n-old\n+new\n",
                    "collapsed": False,
                    "too_large": False,
                }
            ],
        )
    )

    acquired = GitLabInputProvider(security(), Credentials()).acquire_url(
        task_id="task-1",
        source_url="https://gitlab.com/acme/app/merge_requests/9",
    )

    assert acquired.identity.base_sha == "a" * 40
    assert acquired.identity.head_sha == "b" * 40

