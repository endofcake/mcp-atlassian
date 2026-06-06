"""Unit tests for the Bitbucket FastMCP server tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_atlassian.bitbucket.client import BitbucketProjectsPage
from mcp_atlassian.bitbucket.repositories import BitbucketRepositoriesPage
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketRepository
from mcp_atlassian.servers.bitbucket import list_projects, list_repositories

pytestmark = pytest.mark.anyio


class TestListProjectsTool:
    """The list_projects tool's success and error handling."""

    async def test_success_returns_projects(self):
        """A successful call returns the projects plus pagination flags as JSON."""
        fetcher = MagicMock()
        fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[{"key": "PROJ"}], is_last_page=True, truncated=False
        )
        ctx = MagicMock()

        with patch(
            "mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=fetcher),
        ):
            result = await list_projects(ctx, limit=10)

        payload = json.loads(result)
        assert payload["success"] is True
        assert payload["projects"] == [{"key": "PROJ"}]
        assert payload["count"] == 1
        assert payload["is_last_page"] is True
        assert payload["truncated"] is False
        fetcher.list_projects.assert_called_once_with(limit=10)

    async def test_truncated_result_surfaces_flag(self):
        """When the client truncates, the tool reports truncated=true to the caller."""
        fetcher = MagicMock()
        fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[{"key": "PROJ"}], is_last_page=False, truncated=True
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
            repositories=[repo], is_last_page=True, truncated=False
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
        fetcher.list_repositories.assert_called_once_with(project_key="PROJ", limit=10)

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
