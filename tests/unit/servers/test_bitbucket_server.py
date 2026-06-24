"""Unit tests for the Bitbucket FastMCP server tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from mcp_atlassian.bitbucket.client import (
    BitbucketProjectsPage,
    BitbucketResourceNotFoundError,
)
from mcp_atlassian.bitbucket.commits import BitbucketCommitsPage
from mcp_atlassian.bitbucket.pull_requests import (
    BitbucketActivitiesPage,
    BitbucketPullRequestsPage,
)
from mcp_atlassian.bitbucket.refs import (
    BitbucketBranchesPage,
    BitbucketTagsPage,
)
from mcp_atlassian.bitbucket.repositories import BitbucketRepositoriesPage
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import (
    BitbucketActivity,
    BitbucketBranch,
    BitbucketComment,
    BitbucketCommit,
    BitbucketProject,
    BitbucketPullRequest,
    BitbucketPullRequestDiff,
    BitbucketRepository,
    BitbucketTag,
)
from mcp_atlassian.servers.bitbucket import (
    add_comment,
    delete_comment,
    edit_comment,
    get_commit,
    get_default_branch,
    get_pull_request,
    get_pull_request_activities,
    get_pull_request_comments,
    get_pull_request_commits,
    get_pull_request_diff,
    get_tag,
    list_branches,
    list_commits,
    list_projects,
    list_pull_requests,
    list_repositories,
    list_tags,
    resolve_comment,
    set_review_status,
)

pytestmark = pytest.mark.anyio


class TestListProjectsTool:
    """The list_projects tool's success and error handling."""

    async def test_success_returns_projects(self):
        """A successful call returns the projects plus pagination flags as JSON."""
        fetcher = MagicMock()
        # list_projects now emits model-simplified dicts (snake_case, no raw
        # id/links), consistent with list_repositories — see the migration.
        project = BitbucketProject.from_api_response({"key": "PROJ", "name": "Proj"})
        fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[project], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_projects(ctx, limit=10)

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["projects"] == [{"key": "PROJ", "name": "Proj"}]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True
        assert payload["truncated"] is False
        assert payload["next_page_start"] is None
        fetcher.list_projects.assert_called_once_with(name=None, start=0, limit=10)

    async def test_summary_returns_identity_only(self):
        """summary=True projects each item to its identity fields only."""
        fetcher = MagicMock()
        project = BitbucketProject.from_api_response(
            {"key": "PROJ", "name": "Proj", "description": "d", "type": "NORMAL"}
        )
        fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[project], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_projects(ctx, summary=True)

        payload = json.loads(result)
        assert payload["projects"] == [{"key": "PROJ", "name": "Proj"}]
        assert "description" not in payload["projects"][0]
        # Projection is a tool-layer concern; the mixin call carries no summary.
        assert "summary" not in fetcher.list_projects.call_args.kwargs

    async def test_start_cursor_is_passed_and_surfaced(self):
        """The start cursor is forwarded and next_page_start is surfaced."""
        fetcher = MagicMock()
        project = BitbucketProject.from_api_response({"key": "PROJ", "name": "Proj"})
        fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[project], is_last_page=False, truncated=True, next_page_start=25
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_projects(ctx, start=10, limit=25)

        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        fetcher.list_projects.assert_called_once_with(name=None, start=10, limit=25)

    async def test_name_filter_threads_to_mixin(self):
        """A supplied name is forwarded to the mixin call."""
        fetcher = MagicMock()
        fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            await list_projects(ctx, name="Proj")

        assert fetcher.list_projects.call_args[1]["name"] == "Proj"

    async def test_truncated_result_surfaces_flag(self):
        """When the client truncates, the tool reports truncated=true to the caller."""
        fetcher = MagicMock()
        fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[BitbucketProject.from_api_response({"key": "PROJ"})],
            is_last_page=False,
            truncated=True,
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_projects(ctx)

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["truncated"] is True
        assert payload["is_last_page"] is False

    async def test_fetcher_resolution_failure_is_handled(self):
        """A failure resolving the fetcher is caught, not propagated.

        get_bitbucket_fetcher is called inside the try, so a resolution
        ValueError yields an error object rather than an unhandled exception.
        """
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(side_effect=ValueError("Bitbucket client not available")),
        ):
            result = await list_projects(ctx)

        payload = json.loads(result)
        assert payload["success"] is False
        assert "error" in payload

    async def test_unexpected_error_returns_sanitised_message(self):
        """The client gets the sanitised message, not the raw exception text."""
        fetcher = MagicMock()
        fetcher.list_projects.side_effect = RuntimeError(
            "secret-internal-host:5432 connection detail"
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_projects(ctx)

        payload = json.loads(result)
        assert payload["success"] is False
        # Sanitised message returned; raw exception detail must not leak.
        assert payload["error"] == (
            "An unexpected error occurred while listing projects."
        )
        assert "secret-internal-host" not in payload["error"]

    async def test_unexpected_error_logged_server_side(self, caplog):
        """The detailed exception text is logged server-side for diagnostics."""
        fetcher = MagicMock()
        fetcher.list_projects.side_effect = RuntimeError("detailed failure cause")
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            with caplog.at_level("ERROR"):
                await list_projects(ctx)

        assert "detailed failure cause" in caplog.text

    async def test_auth_error_message(self):
        """An authentication error is surfaced with the auth prefix."""
        fetcher = MagicMock()
        fetcher.list_projects.side_effect = MCPAtlassianAuthenticationError(
            "token rejected"
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_projects(ctx)

        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_network_error_message(self, caplog):
        """A network/API failure is surfaced as a network error, not 'unexpected'.

        The fetcher raises ValueError for connection failures, non-auth HTTP
        statuses, and non-JSON responses. Those must be classified distinctly
        and logged without a traceback, not bucketed as an unexpected error.
        """
        fetcher = MagicMock()
        fetcher.list_projects.side_effect = ValueError(
            "Bitbucket API request to /projects failed with HTTP 500."
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            with caplog.at_level("ERROR"):
                result = await list_projects(ctx)

        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Network or API Error:")
        assert payload["error"] != (
            "An unexpected error occurred while listing projects."
        )
        # Logged as a single error line, not an exception traceback.
        assert "Traceback" not in caplog.text


class TestListRepositoriesTool:
    """The list_repositories tool's success and error handling."""

    async def test_success_returns_repositories(self):
        """A successful call returns simplified repositories plus pagination flags."""
        fetcher = MagicMock()
        repo = BitbucketRepository.from_api_response(
            {"slug": "api", "name": "api", "project": {"key": "PROJ", "name": "PROJ"}}
        )
        fetcher.list_repositories.return_value = BitbucketRepositoriesPage(
            repositories=[repo],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(ctx, project_key="PROJ", limit=10)

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["repositories"] == [repo.to_simplified_dict()]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True
        assert payload["truncated"] is False
        assert payload["next_page_start"] is None
        fetcher.list_repositories.assert_called_once_with(
            project_key="PROJ", name=None, start=0, limit=10
        )

    async def test_summary_returns_identity_only(self):
        """summary=True projects each repository to slug + name only."""
        fetcher = MagicMock()
        repo = BitbucketRepository.from_api_response(
            {
                "slug": "api",
                "name": "api",
                "description": "d",
                "project": {"key": "PROJ", "name": "PROJ"},
            }
        )
        fetcher.list_repositories.return_value = BitbucketRepositoriesPage(
            repositories=[repo],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(ctx, project_key="PROJ", summary=True)

        payload = json.loads(result)
        assert payload["repositories"] == [{"slug": "api", "name": "api"}]
        assert "project" not in payload["repositories"][0]
        assert "summary" not in fetcher.list_repositories.call_args.kwargs

    async def test_start_cursor_is_passed_and_surfaced(self):
        """The start cursor is forwarded and next_page_start is surfaced."""
        fetcher = MagicMock()
        repo = BitbucketRepository.from_api_response(
            {"slug": "api", "name": "api", "project": {"key": "PROJ", "name": "PROJ"}}
        )
        fetcher.list_repositories.return_value = BitbucketRepositoriesPage(
            repositories=[repo],
            is_last_page=False,
            truncated=True,
            next_page_start=25,
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(
                ctx, project_key="PROJ", start=10, limit=25
            )

        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        fetcher.list_repositories.assert_called_once_with(
            project_key="PROJ", name=None, start=10, limit=25
        )

    async def test_name_filter_threads_to_mixin(self):
        """A supplied name is forwarded to the mixin call."""
        fetcher = MagicMock()
        fetcher.list_repositories.return_value = BitbucketRepositoriesPage(
            repositories=[], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            await list_repositories(ctx, project_key="PROJ", name="api")

        assert fetcher.list_repositories.call_args[1]["name"] == "api"

    async def test_truncated_result_surfaces_flag(self):
        """When the client truncates, the tool reports truncated=true."""
        fetcher = MagicMock()
        fetcher.list_repositories.return_value = BitbucketRepositoriesPage(
            repositories=[], is_last_page=False, truncated=True
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(ctx, project_key="PROJ")

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["truncated"] is True
        assert payload["is_last_page"] is False

    async def test_validation_error_is_network_error(self):
        """A blank-key ValueError from the fetcher is a classified API error."""
        fetcher = MagicMock()
        fetcher.list_repositories.side_effect = ValueError(
            "project_key must be a non-empty Bitbucket project key."
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(ctx, project_key="  ")

        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Network or API Error:")

    async def test_auth_error_message(self):
        """An authentication error is surfaced with the auth prefix."""
        fetcher = MagicMock()
        fetcher.list_repositories.side_effect = MCPAtlassianAuthenticationError(
            "token rejected"
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(ctx, project_key="PROJ")

        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_unexpected_error_returns_sanitised_message(self):
        """The client gets the sanitised message, not the raw exception text."""
        fetcher = MagicMock()
        fetcher.list_repositories.side_effect = RuntimeError(
            "secret-internal-host:5432 connection detail"
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(ctx, project_key="PROJ")

        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"] == (
            "An unexpected error occurred while listing repositories."
        )
        assert "secret-internal-host" not in payload["error"]

    async def test_not_found_error_message(self):
        """A 404 from the fetcher is surfaced as an actionable 'Not Found' error.

        BitbucketResourceNotFoundError subclasses ValueError; the dedicated
        branch must classify it as not-found, not as a generic network error.
        """
        fetcher = MagicMock()
        fetcher.list_repositories.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for /projects/NOPE/repos. "
            "The project, repository, or pull request does not exist, or the "
            "authenticated user lacks permission to view it."
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_repositories(ctx, project_key="NOPE")

        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Not Found:")
        assert not payload["error"].startswith("Network or API Error:")


def _patched_fetcher(fetcher):
    """Patch get_bitbucket_fetcher to return the given fetcher mock."""
    return patch(
        "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
        AsyncMock(return_value=fetcher),
    )


_PR_API = {
    "id": 5,
    "title": "Add X",
    "state": "OPEN",
    "participants": [{"user": {"name": "a"}, "role": "AUTHOR"}],
    "reviewers": [{"user": {"name": "r"}, "status": "APPROVED", "approved": True}],
}

_BRANCH_API = {
    "id": "refs/heads/main",
    "displayId": "main",
    "latestCommit": "abc123",
    "type": "BRANCH",
    "default": True,
}

_TAG_API = {
    "id": "refs/tags/v1.0.0",
    "displayId": "v1.0.0",
    "latestCommit": "def456",
    "type": "TAG",
    "hash": "objsha",
}


class TestListBranchesTool:
    """The list_branches tool."""

    async def test_success_returns_simplified_branches(self):
        fetcher = MagicMock()
        branch = BitbucketBranch.from_api_response(_BRANCH_API)
        fetcher.list_branches.return_value = BitbucketBranchesPage(
            branches=[branch],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_branches(
                ctx, project_key="PROJ", repository_slug="my-repo"
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["branches"] == [branch.to_simplified_dict()]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True
        assert payload["truncated"] is False
        assert payload["next_page_start"] is None

    async def test_summary_returns_triage_only(self):
        fetcher = MagicMock()
        branch = BitbucketBranch.from_api_response(_BRANCH_API)
        fetcher.list_branches.return_value = BitbucketBranchesPage(
            branches=[branch], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_branches(
                ctx, project_key="PROJ", repository_slug="my-repo", summary=True
            )
        payload = json.loads(result)
        assert payload["branches"] == [
            {"display_id": "main", "latest_commit": "abc123"}
        ]
        # Projection is a tool-layer concern; the mixin call carries no summary.
        assert "summary" not in fetcher.list_branches.call_args.kwargs

    async def test_filters_and_cursor_thread_to_mixin(self):
        fetcher = MagicMock()
        fetcher.list_branches.return_value = BitbucketBranchesPage(
            branches=[], is_last_page=False, truncated=True, next_page_start=25
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_branches(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                filter_text="main",
                order_by="MODIFICATION",
                boost_matches=True,
                start=10,
                limit=50,
            )
        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        fetcher.list_branches.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            filter_text="main",
            order_by="MODIFICATION",
            boost_matches=True,
            start=10,
            limit=50,
        )

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.list_branches.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../branches."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_branches(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.list_branches.side_effect = MCPAtlassianAuthenticationError("401")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_branches(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.list_branches.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_branches(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"] == (
            "An unexpected error occurred while listing branches."
        )
        assert "secret-host" not in payload["error"]


class TestListTagsTool:
    """The list_tags tool."""

    async def test_success_returns_simplified_tags(self):
        fetcher = MagicMock()
        tag = BitbucketTag.from_api_response(_TAG_API)
        fetcher.list_tags.return_value = BitbucketTagsPage(
            tags=[tag], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_tags(ctx, project_key="PROJ", repository_slug="my-repo")
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["tags"] == [tag.to_simplified_dict()]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True

    async def test_summary_returns_triage_only(self):
        fetcher = MagicMock()
        tag = BitbucketTag.from_api_response(_TAG_API)
        fetcher.list_tags.return_value = BitbucketTagsPage(
            tags=[tag], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_tags(
                ctx, project_key="PROJ", repository_slug="my-repo", summary=True
            )
        payload = json.loads(result)
        assert payload["tags"] == [{"display_id": "v1.0.0", "latest_commit": "def456"}]
        assert "summary" not in fetcher.list_tags.call_args.kwargs

    async def test_filters_and_cursor_thread_to_mixin(self):
        fetcher = MagicMock()
        fetcher.list_tags.return_value = BitbucketTagsPage(
            tags=[], is_last_page=False, truncated=True, next_page_start=25
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_tags(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                filter_text="v1",
                order_by="ALPHABETICAL",
                start=10,
                limit=50,
            )
        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        fetcher.list_tags.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            filter_text="v1",
            order_by="ALPHABETICAL",
            start=10,
            limit=50,
        )

    async def test_network_error_message(self):
        fetcher = MagicMock()
        fetcher.list_tags.side_effect = ValueError("boom")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_tags(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Network or API Error:")

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.list_tags.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../tags."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_tags(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.list_tags.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_tags(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"] == ("An unexpected error occurred while listing tags.")
        assert "secret-host" not in payload["error"]


class TestGetTagTool:
    """The get_tag tool."""

    async def test_success_returns_tag(self):
        fetcher = MagicMock()
        fetcher.get_tag.return_value = BitbucketTag.from_api_response(_TAG_API)
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_tag(
                ctx, project_key="PROJ", repository_slug="my-repo", name="v1.0.0"
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["tag"]["display_id"] == "v1.0.0"
        assert payload["tag"]["hash"] == "objsha"
        fetcher.get_tag.assert_called_once_with(
            project_key="PROJ", repository_slug="my-repo", name="v1.0.0"
        )

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.get_tag.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../tags/v-missing."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_tag(
                ctx, project_key="P", repository_slug="r", name="v-missing"
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.get_tag.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_tag(ctx, project_key="P", repository_slug="r", name="v1")
        payload = json.loads(result)
        assert payload["error"] == (
            "An unexpected error occurred while getting the tag."
        )
        assert "secret-host" not in payload["error"]


class TestGetDefaultBranchTool:
    """The get_default_branch tool."""

    async def test_success_returns_branch(self):
        fetcher = MagicMock()
        fetcher.get_default_branch.return_value = BitbucketBranch.from_api_response(
            {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH"}
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_default_branch(
                ctx, project_key="PROJ", repository_slug="my-repo"
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["branch"]["display_id"] == "main"
        assert payload["branch"]["id"] == "refs/heads/main"
        fetcher.get_default_branch.assert_called_once_with(
            project_key="PROJ", repository_slug="my-repo"
        )

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.get_default_branch.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../default-branch."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_default_branch(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.get_default_branch.side_effect = MCPAtlassianAuthenticationError("403")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_default_branch(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_network_error_message(self):
        fetcher = MagicMock()
        fetcher.get_default_branch.side_effect = ValueError("boom")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_default_branch(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Network or API Error:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.get_default_branch.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_default_branch(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"] == (
            "An unexpected error occurred while getting the default branch."
        )
        assert "secret-host" not in payload["error"]


class TestListPullRequestsTool:
    """The list_pull_requests tool."""

    async def test_success_returns_simplified_prs(self):
        fetcher = MagicMock()
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        fetcher.list_pull_requests.return_value = BitbucketPullRequestsPage(
            pull_requests=[pr],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()

        with _patched_fetcher(fetcher):
            result = await list_pull_requests(
                ctx, project_key="PROJ", repository_slug="my-repo", state="MERGED"
            )

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["pull_requests"] == [pr.to_simplified_dict()]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True
        assert payload["next_page_start"] is None
        fetcher.list_pull_requests.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            state="MERGED",
            direction=None,
            at=None,
            order=None,
            filter_text=None,
            draft=None,
            start=0,
            limit=25,
        )

    async def test_summary_returns_triage_fields(self):
        """summary=True projects each PR to its triage fields only."""
        fetcher = MagicMock()
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        fetcher.list_pull_requests.return_value = BitbucketPullRequestsPage(
            pull_requests=[pr],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()

        with _patched_fetcher(fetcher):
            result = await list_pull_requests(
                ctx, project_key="PROJ", repository_slug="my-repo", summary=True
            )

        payload = json.loads(result)
        assert payload["pull_requests"] == [
            {
                "id": 5,
                "title": "Add X",
                "state": "OPEN",
                "author": {"user": {"name": "a"}, "role": "AUTHOR"},
            }
        ]
        assert "summary" not in fetcher.list_pull_requests.call_args.kwargs

    async def test_start_cursor_is_passed_and_surfaced(self):
        """The start cursor is forwarded and next_page_start is surfaced."""
        fetcher = MagicMock()
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        fetcher.list_pull_requests.return_value = BitbucketPullRequestsPage(
            pull_requests=[pr],
            is_last_page=False,
            truncated=True,
            next_page_start=25,
        )
        ctx = MagicMock()

        with _patched_fetcher(fetcher):
            result = await list_pull_requests(
                ctx,
                project_key="P",
                repository_slug="r",
                start=10,
                limit=25,
            )

        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        assert fetcher.list_pull_requests.call_args[1]["start"] == 10

    async def test_filter_text_and_draft_thread_to_mixin(self):
        """Supplied filter_text and draft are forwarded to the mixin call."""
        fetcher = MagicMock()
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        fetcher.list_pull_requests.return_value = BitbucketPullRequestsPage(
            pull_requests=[pr],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()

        with _patched_fetcher(fetcher):
            await list_pull_requests(
                ctx,
                project_key="P",
                repository_slug="r",
                filter_text="login",
                draft=True,
            )

        call_kwargs = fetcher.list_pull_requests.call_args[1]
        assert call_kwargs["filter_text"] == "login"
        assert call_kwargs["draft"] is True

    async def test_absent_filters_default_to_none(self):
        """filter_text and draft default to None when not supplied."""
        fetcher = MagicMock()
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        fetcher.list_pull_requests.return_value = BitbucketPullRequestsPage(
            pull_requests=[pr],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()

        with _patched_fetcher(fetcher):
            await list_pull_requests(ctx, project_key="P", repository_slug="r")

        call_kwargs = fetcher.list_pull_requests.call_args[1]
        assert call_kwargs["filter_text"] is None
        assert call_kwargs["draft"] is None

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.list_pull_requests.side_effect = MCPAtlassianAuthenticationError(
            "rejected"
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_pull_requests(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")


class TestGetPullRequestTool:
    """The get_pull_request tool."""

    async def test_success_returns_pr(self):
        fetcher = MagicMock()
        fetcher.get_pull_request.return_value = BitbucketPullRequest.from_api_response(
            _PR_API
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request(
                ctx, project_key="PROJ", repository_slug="my-repo", pull_request_id=5
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["pull_request"]["id"] == 5

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.get_pull_request.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../pull-requests/999."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request(
                ctx, project_key="P", repository_slug="r", pull_request_id=999
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.get_pull_request.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request(
                ctx, project_key="P", repository_slug="r", pull_request_id=1
            )
        payload = json.loads(result)
        assert payload["error"] == (
            "An unexpected error occurred while getting the pull request."
        )
        assert "secret-host" not in payload["error"]


class TestGetPullRequestDiffTool:
    """The get_pull_request_diff tool."""

    async def test_success_returns_structured_diff(self):
        fetcher = MagicMock()
        diff = BitbucketPullRequestDiff.from_api_response(
            {
                "diffs": [
                    {
                        "destination": {"name": "f.py"},
                        "hunks": [
                            {"segments": [{"type": "ADDED", "lines": [{"line": "x"}]}]}
                        ],
                    }
                ]
            }
        )
        fetcher.get_pull_request_diff.return_value = diff
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_diff(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                max_lines_per_file=200,
                max_files=10,
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["diff"]["count"] == 1
        assert payload["diff"]["truncated"] is False
        # Both token-control caps are forwarded to the fetcher.
        fetcher.get_pull_request_diff.assert_called_once_with(
            project_key="P",
            repository_slug="r",
            pull_request_id=5,
            max_lines_per_file=200,
            max_files=10,
        )

    async def test_network_error_message(self):
        fetcher = MagicMock()
        fetcher.get_pull_request_diff.side_effect = ValueError("HTTP 500")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_diff(
                ctx, project_key="P", repository_slug="r", pull_request_id=5
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Network or API Error:")


class TestGetPullRequestActivitiesTool:
    """The get_pull_request_activities and get_pull_request_comments tools."""

    async def test_activities_success(self):
        fetcher = MagicMock()
        activity = BitbucketActivity.from_api_response(
            {"id": 1, "action": "APPROVED", "user": {"name": "r"}}
        )
        fetcher.get_activities.return_value = BitbucketActivitiesPage(
            activities=[activity],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_activities(
                ctx, project_key="P", repository_slug="r", pull_request_id=5
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["activities"][0]["action"] == "APPROVED"
        assert payload["next_page_start"] is None
        # The activities tool does not filter by action and starts at 0.
        assert fetcher.get_activities.call_args[1].get("action") is None
        assert fetcher.get_activities.call_args[1]["start"] == 0

    async def test_activities_start_cursor_is_passed_and_surfaced(self):
        """The start cursor is forwarded and next_page_start is surfaced."""
        fetcher = MagicMock()
        activity = BitbucketActivity.from_api_response(
            {"id": 1, "action": "APPROVED", "user": {"name": "r"}}
        )
        fetcher.get_activities.return_value = BitbucketActivitiesPage(
            activities=[activity],
            is_last_page=False,
            truncated=True,
            next_page_start=25,
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_activities(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                start=10,
            )
        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        assert fetcher.get_activities.call_args[1]["start"] == 10

    async def test_comments_filters_to_commented_and_extracts_comment(self):
        fetcher = MagicMock()
        commented = BitbucketActivity.from_api_response(
            {
                "id": 1,
                "action": "COMMENTED",
                "user": {"name": "r"},
                "comment": {"id": 9, "text": "nit", "author": {"name": "r"}},
            }
        )
        fetcher.get_activities.return_value = BitbucketActivitiesPage(
            activities=[commented], is_last_page=True, truncated=False
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_comments(
                ctx, project_key="P", repository_slug="r", pull_request_id=5
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["count"] == 1
        assert payload["comments"][0]["text"] == "nit"
        # The comments tool drives the same mixin method with a COMMENTED filter.
        assert fetcher.get_activities.call_args[1]["action"] == "COMMENTED"

    async def test_comments_drops_commented_without_payload(self):
        """A COMMENTED activity with no comment object is dropped (count 0).

        This is indistinguishable from "no comments exist" — pinned so a future
        change does not silently alter it.
        """
        fetcher = MagicMock()
        payloadless = BitbucketActivity.from_api_response(
            {"id": 1, "action": "COMMENTED", "user": {"name": "r"}}
        )
        fetcher.get_activities.return_value = BitbucketActivitiesPage(
            activities=[payloadless], is_last_page=True, truncated=False
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_comments(
                ctx, project_key="P", repository_slug="r", pull_request_id=5
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["comments"] == []
        assert payload["count"] == 0

    async def test_comments_count_may_differ_and_passes_truncated(self):
        """limit bounds activities scanned, not comments returned.

        With one payload-bearing and one payload-less COMMENTED activity, the
        comment count (1) is below the activity count (2), and the client's
        truncated/is_last_page flags pass through unchanged.
        """
        fetcher = MagicMock()
        with_payload = BitbucketActivity.from_api_response(
            {
                "id": 1,
                "action": "COMMENTED",
                "user": {"name": "r"},
                "comment": {"id": 9, "text": "x", "author": {"name": "r"}},
            }
        )
        without_payload = BitbucketActivity.from_api_response(
            {"id": 2, "action": "COMMENTED", "user": {"name": "r"}}
        )
        fetcher.get_activities.return_value = BitbucketActivitiesPage(
            activities=[with_payload, without_payload],
            is_last_page=False,
            truncated=True,
            next_page_start=25,
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_comments(
                ctx, project_key="P", repository_slug="r", pull_request_id=5, start=10
            )
        payload = json.loads(result)
        assert payload["count"] == 1
        assert payload["truncated"] is True
        assert payload["is_last_page"] is False
        # The blind-paging cursor is surfaced and start is forwarded.
        assert payload["next_page_start"] == 25
        assert fetcher.get_activities.call_args[1]["start"] == 10


def _read_only_ctx() -> MagicMock:
    """A context whose lifespan reports READ_ONLY_MODE, for the write gate."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "app_lifespan_context": MagicMock(read_only=True)
    }
    return ctx


class TestAddCommentTool:
    """The add_comment write tool: success, the read-only gate, error mapping."""

    async def test_success_returns_created_comment(self):
        fetcher = MagicMock()
        fetcher.add_comment.return_value = BitbucketComment.from_api_response(
            {"id": 101, "version": 0, "text": "hello"}
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await add_comment(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                pull_request_id=5,
                text="hello",
            )

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["comment"]["id"] == 101
        assert payload["comment"]["version"] == 0
        fetcher.add_comment.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            text="hello",
            parent_id=None,
            file_path=None,
            line=None,
            line_type=None,
            file_type=None,
            severity="NORMAL",
        )

    async def test_read_only_mode_blocks_and_makes_no_call(self):
        """READ_ONLY_MODE raises a ToolError before any fetcher/HTTP call."""
        fetcher = MagicMock()
        with _patched_fetcher(fetcher):
            with pytest.raises(ToolError, match="read-only mode"):
                await add_comment(
                    _read_only_ctx(),
                    project_key="P",
                    repository_slug="r",
                    pull_request_id=5,
                    text="x",
                )
        fetcher.add_comment.assert_not_called()

    async def test_validation_error_is_network_error(self):
        """A client-side ValueError surfaces as the sanitised network error."""
        fetcher = MagicMock()
        fetcher.add_comment.side_effect = ValueError("text must be a non-blank string.")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await add_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                text="  ",
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Network or API Error:")

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.add_comment.side_effect = BitbucketResourceNotFoundError("nope")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await add_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=999,
                text="x",
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.add_comment.side_effect = MCPAtlassianAuthenticationError("rejected")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await add_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                text="x",
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")


class TestSetReviewStatusTool:
    """The set_review_status write tool: success, gate, and error mapping."""

    async def test_success_returns_confirmed_status(self):
        fetcher = MagicMock()
        fetcher.set_review_status.return_value = {
            "status": "APPROVED",
            "user": {"name": "me"},
        }
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await set_review_status(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                pull_request_id=5,
                status="APPROVED",
            )

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["participant"]["status"] == "APPROVED"
        fetcher.set_review_status.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            status="APPROVED",
        )

    async def test_read_only_mode_blocks_and_makes_no_call(self):
        fetcher = MagicMock()
        with _patched_fetcher(fetcher):
            with pytest.raises(ToolError, match="read-only mode"):
                await set_review_status(
                    _read_only_ctx(),
                    project_key="P",
                    repository_slug="r",
                    pull_request_id=5,
                    status="APPROVED",
                )
        fetcher.set_review_status.assert_not_called()

    async def test_author_rejection_is_network_error(self):
        """The author-cannot-set-status rejection surfaces as a sanitised error."""
        fetcher = MagicMock()
        fetcher.set_review_status.side_effect = ValueError(
            "Bitbucket API request to ... failed with HTTP 409: "
            "The author of a pull request may not change status."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await set_review_status(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                status="APPROVED",
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Network or API Error:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.set_review_status.side_effect = MCPAtlassianAuthenticationError(
            "rejected"
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await set_review_status(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                status="NEEDS_WORK",
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")


class TestEditCommentTool:
    """The edit_comment write tool: success, the read-only gate, error mapping."""

    async def test_success_returns_updated_comment(self):
        fetcher = MagicMock()
        fetcher.update_comment.return_value = BitbucketComment.from_api_response(
            {"id": 9, "version": 4, "text": "edited"}
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await edit_comment(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                pull_request_id=5,
                comment_id=9,
                text="edited",
                version=3,
            )

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["comment"]["id"] == 9
        assert payload["comment"]["version"] == 4
        fetcher.update_comment.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            comment_id=9,
            version=3,
            text="edited",
        )

    async def test_read_only_mode_blocks_and_makes_no_call(self):
        """READ_ONLY_MODE raises a ToolError before any fetcher/HTTP call."""
        fetcher = MagicMock()
        with _patched_fetcher(fetcher):
            with pytest.raises(ToolError, match="read-only mode"):
                await edit_comment(
                    _read_only_ctx(),
                    project_key="P",
                    repository_slug="r",
                    pull_request_id=5,
                    comment_id=9,
                    text="x",
                    version=3,
                )
        fetcher.update_comment.assert_not_called()

    async def test_stale_version_is_network_error(self):
        """A 409 stale-version ValueError surfaces as the sanitised error."""
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = ValueError(
            "Bitbucket API request to ... failed with HTTP 409: "
            "The comment version is out of date."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await edit_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                text="x",
                version=1,
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Network or API Error:")

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = BitbucketResourceNotFoundError("nope")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await edit_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=999,
                text="x",
                version=3,
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_auth_error_message(self):
        """Author-only-text-edit rejection surfaces as the auth/permission error."""
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = MCPAtlassianAuthenticationError("rejected")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await edit_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                text="x",
                version=3,
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_unexpected_error_is_sanitised(self, caplog):
        """An unexpected error is logged but never leaked to the client."""
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = RuntimeError("pool=secret host=internal")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await edit_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                text="x",
                version=3,
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert "secret" not in payload["error"]
        assert payload["error"] == (
            "An unexpected error occurred while editing the comment."
        )


class TestResolveCommentTool:
    """The resolve_comment write tool: resolve/reopen, the gate, error mapping."""

    async def test_resolve_threads_resolved_true(self):
        fetcher = MagicMock()
        fetcher.update_comment.return_value = BitbucketComment.from_api_response(
            {"id": 9, "version": 4, "threadResolved": True}
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await resolve_comment(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                pull_request_id=5,
                comment_id=9,
                version=3,
            )

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["comment"]["thread_resolved"] is True
        fetcher.update_comment.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            comment_id=9,
            version=3,
            thread_resolved=True,
        )

    async def test_unresolve_passes_thread_resolved_false(self):
        """resolved=False reopens the thread (threadResolved=False)."""
        fetcher = MagicMock()
        fetcher.update_comment.return_value = BitbucketComment.from_api_response(
            {"id": 9, "version": 5, "threadResolved": False}
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await resolve_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                version=4,
                resolved=False,
            )

        payload = json.loads(result)
        assert payload["comment"]["thread_resolved"] is False
        fetcher.update_comment.assert_called_once_with(
            project_key="P",
            repository_slug="r",
            pull_request_id=5,
            comment_id=9,
            version=4,
            thread_resolved=False,
        )

    async def test_read_only_mode_blocks_and_makes_no_call(self):
        fetcher = MagicMock()
        with _patched_fetcher(fetcher):
            with pytest.raises(ToolError, match="read-only mode"):
                await resolve_comment(
                    _read_only_ctx(),
                    project_key="P",
                    repository_slug="r",
                    pull_request_id=5,
                    comment_id=9,
                    version=3,
                )
        fetcher.update_comment.assert_not_called()

    async def test_stale_version_is_network_error(self):
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = ValueError(
            "failed with HTTP 409: out of date"
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await resolve_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                version=1,
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Network or API Error:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = MCPAtlassianAuthenticationError("rejected")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await resolve_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                version=3,
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = BitbucketResourceNotFoundError("nope")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await resolve_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=999,
                version=3,
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_unexpected_error_is_sanitised(self):
        """An unexpected error is sanitised — internals never reach the client."""
        fetcher = MagicMock()
        fetcher.update_comment.side_effect = RuntimeError("pool=secret host=internal")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await resolve_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                version=3,
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert "secret" not in payload["error"]
        assert payload["error"] == (
            "An unexpected error occurred while resolving the comment."
        )


class TestDeleteCommentTool:
    """The delete_comment write tool: success envelope, the gate, error mapping."""

    async def test_success_echoes_comment_id(self):
        """A successful delete (204, no body) confirms with the comment id."""
        fetcher = MagicMock()
        fetcher.delete_comment.return_value = None
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await delete_comment(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                pull_request_id=5,
                comment_id=9,
                version=3,
            )

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["comment_id"] == 9
        fetcher.delete_comment.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            comment_id=9,
            version=3,
        )

    async def test_read_only_mode_blocks_and_makes_no_call(self):
        fetcher = MagicMock()
        with _patched_fetcher(fetcher):
            with pytest.raises(ToolError, match="read-only mode"):
                await delete_comment(
                    _read_only_ctx(),
                    project_key="P",
                    repository_slug="r",
                    pull_request_id=5,
                    comment_id=9,
                    version=3,
                )
        fetcher.delete_comment.assert_not_called()

    async def test_has_replies_409_is_network_error(self):
        """A 409 (has-replies/stale) ValueError surfaces as the sanitised error."""
        fetcher = MagicMock()
        fetcher.delete_comment.side_effect = ValueError(
            "failed with HTTP 409: The comment has replies and cannot be deleted."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await delete_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                version=1,
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["error"].startswith("Network or API Error:")

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.delete_comment.side_effect = BitbucketResourceNotFoundError("nope")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await delete_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=999,
                version=3,
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_unexpected_error_is_sanitised(self):
        """An unexpected error is sanitised — internals never reach the client."""
        fetcher = MagicMock()
        fetcher.delete_comment.side_effect = RuntimeError("pool=secret host=internal")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await delete_comment(
                ctx,
                project_key="P",
                repository_slug="r",
                pull_request_id=5,
                comment_id=9,
                version=3,
            )
        payload = json.loads(result)
        assert payload["success"] is False
        assert "secret" not in payload["error"]
        assert payload["error"] == (
            "An unexpected error occurred while deleting the comment."
        )


_COMMIT_API = {
    "id": "abc123def456",
    "displayId": "abc123d",
    "message": "Add X\n\nbody",
    "author": {"name": "Jane Dev", "emailAddress": "jane@x"},
    "authorTimestamp": 1700000000000,
    "committer": {"name": "Jane Dev", "emailAddress": "jane@x"},
    "committerTimestamp": 1700000000000,
    "parents": [{"id": "p1", "displayId": "p1d"}],
}


class TestListCommitsTool:
    """The list_commits tool."""

    async def test_success_returns_simplified_commits(self):
        fetcher = MagicMock()
        commit = BitbucketCommit.from_api_response(_COMMIT_API)
        fetcher.list_commits.return_value = BitbucketCommitsPage(
            commits=[commit],
            is_last_page=True,
            truncated=False,
            next_page_start=None,
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_commits(
                ctx, project_key="PROJ", repository_slug="my-repo"
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["commits"] == [commit.to_simplified_dict()]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True
        assert payload["truncated"] is False
        assert payload["next_page_start"] is None
        # PII minimisation holds end-to-end: no inline email in the envelope.
        assert "jane@x" not in result

    async def test_summary_returns_triage_only(self):
        fetcher = MagicMock()
        commit = BitbucketCommit.from_api_response(_COMMIT_API)
        fetcher.list_commits.return_value = BitbucketCommitsPage(
            commits=[commit], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_commits(
                ctx, project_key="PROJ", repository_slug="my-repo", summary=True
            )
        payload = json.loads(result)
        assert payload["commits"] == [
            {
                "display_id": "abc123d",
                "author": "Jane Dev",
                "author_timestamp": 1700000000000,
                "message": "Add X",
            }
        ]
        # Projection is a tool-layer concern; the mixin call carries no summary.
        assert "summary" not in fetcher.list_commits.call_args.kwargs

    async def test_filters_and_cursor_thread_to_mixin(self):
        fetcher = MagicMock()
        fetcher.list_commits.return_value = BitbucketCommitsPage(
            commits=[], is_last_page=False, truncated=True, next_page_start=25
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_commits(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                since="oldsha",
                until="main",
                path="src/app.py",
                merges="only",
                follow_renames=True,
                ignore_missing=False,
                start=10,
                limit=50,
            )
        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        fetcher.list_commits.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            since="oldsha",
            until="main",
            path="src/app.py",
            merges="only",
            follow_renames=True,
            ignore_missing=False,
            start=10,
            limit=50,
        )

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.list_commits.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../commits."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_commits(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_network_error_message(self):
        fetcher = MagicMock()
        fetcher.list_commits.side_effect = ValueError("boom")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_commits(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Network or API Error:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.list_commits.side_effect = MCPAtlassianAuthenticationError("401")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_commits(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.list_commits.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await list_commits(ctx, project_key="P", repository_slug="r")
        payload = json.loads(result)
        assert payload["error"] == (
            "An unexpected error occurred while listing commits."
        )
        assert "secret-host" not in payload["error"]


class TestGetCommitTool:
    """The get_commit tool."""

    async def test_success_returns_commit(self):
        fetcher = MagicMock()
        fetcher.get_commit.return_value = BitbucketCommit.from_api_response(_COMMIT_API)
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_commit(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                commit_id="abc123def456",
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["commit"]["display_id"] == "abc123d"
        assert payload["commit"]["author"] == {"name": "Jane Dev"}
        fetcher.get_commit.assert_called_once_with(
            project_key="PROJ", repository_slug="my-repo", commit_id="abc123def456"
        )

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.get_commit.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../commits/missing."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_commit(
                ctx, project_key="P", repository_slug="r", commit_id="missing"
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_network_error_message(self):
        fetcher = MagicMock()
        fetcher.get_commit.side_effect = ValueError("boom")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_commit(
                ctx, project_key="P", repository_slug="r", commit_id="sha"
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Network or API Error:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.get_commit.side_effect = MCPAtlassianAuthenticationError("403")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_commit(
                ctx, project_key="P", repository_slug="r", commit_id="sha"
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.get_commit.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_commit(
                ctx, project_key="P", repository_slug="r", commit_id="sha"
            )
        payload = json.loads(result)
        assert payload["error"] == (
            "An unexpected error occurred while getting the commit."
        )
        assert "secret-host" not in payload["error"]


class TestGetPullRequestCommitsTool:
    """The get_pull_request_commits tool."""

    async def test_success_returns_commits(self):
        fetcher = MagicMock()
        commit = BitbucketCommit.from_api_response(_COMMIT_API)
        fetcher.get_pull_request_commits.return_value = BitbucketCommitsPage(
            commits=[commit], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_commits(
                ctx, project_key="PROJ", repository_slug="my-repo", pull_request_id=42
            )
        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["commits"] == [commit.to_simplified_dict()]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True

    async def test_cursor_threads_to_mixin(self):
        fetcher = MagicMock()
        fetcher.get_pull_request_commits.return_value = BitbucketCommitsPage(
            commits=[], is_last_page=False, truncated=True, next_page_start=25
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_commits(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                pull_request_id=42,
                start=10,
                limit=50,
            )
        payload = json.loads(result)
        assert payload["next_page_start"] == 25
        fetcher.get_pull_request_commits.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=42,
            start=10,
            limit=50,
        )

    async def test_summary_returns_triage_only(self):
        fetcher = MagicMock()
        commit = BitbucketCommit.from_api_response(_COMMIT_API)
        fetcher.get_pull_request_commits.return_value = BitbucketCommitsPage(
            commits=[commit], is_last_page=True, truncated=False, next_page_start=None
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_commits(
                ctx,
                project_key="PROJ",
                repository_slug="my-repo",
                pull_request_id=42,
                summary=True,
            )
        payload = json.loads(result)
        assert payload["commits"] == [
            {
                "display_id": "abc123d",
                "author": "Jane Dev",
                "author_timestamp": 1700000000000,
                "message": "Add X",
            }
        ]
        assert "summary" not in fetcher.get_pull_request_commits.call_args.kwargs

    async def test_not_found_message(self):
        fetcher = MagicMock()
        fetcher.get_pull_request_commits.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for .../pull-requests/42/commits."
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_commits(
                ctx, project_key="P", repository_slug="r", pull_request_id=42
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Not Found:")

    async def test_network_error_message(self):
        fetcher = MagicMock()
        fetcher.get_pull_request_commits.side_effect = ValueError("boom")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_commits(
                ctx, project_key="P", repository_slug="r", pull_request_id=42
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Network or API Error:")

    async def test_auth_error_message(self):
        fetcher = MagicMock()
        fetcher.get_pull_request_commits.side_effect = MCPAtlassianAuthenticationError(
            "401"
        )
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_commits(
                ctx, project_key="P", repository_slug="r", pull_request_id=42
            )
        payload = json.loads(result)
        assert payload["error"].startswith("Authentication/Permission Error:")

    async def test_unexpected_error_is_sanitised(self):
        fetcher = MagicMock()
        fetcher.get_pull_request_commits.side_effect = RuntimeError("secret-host:5432")
        ctx = MagicMock()
        with _patched_fetcher(fetcher):
            result = await get_pull_request_commits(
                ctx, project_key="P", repository_slug="r", pull_request_id=42
            )
        payload = json.loads(result)
        assert payload["error"] == (
            "An unexpected error occurred while getting the pull request commits."
        )
        assert "secret-host" not in payload["error"]
