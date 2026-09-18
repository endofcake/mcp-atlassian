"""Unit tests for the Bitbucket FastMCP server."""

import ast
import inspect
import json
import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client import FastMCPTransport
from fastmcp.exceptions import ToolError
from starlette.requests import Request

from src.mcp_atlassian.bitbucket import BitbucketFetcher
from src.mcp_atlassian.bitbucket.builds import BitbucketBuildStatusesPage
from src.mcp_atlassian.bitbucket.client import (
    BitbucketProjectsPage,
    BitbucketResourceNotFoundError,
)
from src.mcp_atlassian.bitbucket.commits import BitbucketCommitsPage
from src.mcp_atlassian.bitbucket.config import BitbucketConfig
from src.mcp_atlassian.bitbucket.pull_requests import (
    BitbucketActivitiesPage,
    BitbucketChangesPage,
    BitbucketPullRequestsPage,
)
from src.mcp_atlassian.bitbucket.refs import (
    BitbucketBranchesPage,
    BitbucketTagsPage,
)
from src.mcp_atlassian.bitbucket.repositories import BitbucketRepositoriesPage
from src.mcp_atlassian.bitbucket.source import BitbucketBrowseResult
from src.mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from src.mcp_atlassian.servers.context import MainAppContext
from src.mcp_atlassian.servers.main import AtlassianMCP
from src.mcp_atlassian.utils.oauth import OAuthConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://bitbucket.example.com"
FULL_SHA = "e00cf62997a027bbf785614a93e2e55bb331d268"


def _model_mock(simplified: dict, summary: dict | None = None) -> MagicMock:
    """Create a model mock exposing to_simplified_dict/to_summary_dict."""
    model = MagicMock()
    model.to_simplified_dict.return_value = simplified
    if summary is not None:
        model.to_summary_dict.return_value = summary
    return model


@pytest.fixture
def mock_bitbucket_fetcher() -> MagicMock:
    """Create a mocked BitbucketFetcher instance for testing."""
    mock_fetcher = MagicMock(spec=BitbucketFetcher)

    project = _model_mock(
        {"key": "PROJ", "name": "Project", "description": "A project"},
        {"key": "PROJ", "name": "Project"},
    )
    mock_fetcher.list_projects.return_value = BitbucketProjectsPage(
        projects=[project], is_last_page=True, truncated=False, next_page_start=None
    )

    repo = _model_mock(
        {"slug": "my-repo", "name": "my-repo", "project": {"key": "PROJ"}},
        {"slug": "my-repo", "name": "my-repo"},
    )
    mock_fetcher.list_repositories.return_value = BitbucketRepositoriesPage(
        repositories=[repo], is_last_page=True, truncated=False, next_page_start=None
    )

    branch = _model_mock(
        {"id": "refs/heads/main", "display_id": "main", "latest_commit": "abc123"},
        {"display_id": "main", "latest_commit": "abc123"},
    )
    mock_fetcher.list_branches.return_value = BitbucketBranchesPage(
        branches=[branch], is_last_page=True, truncated=False, next_page_start=None
    )
    mock_fetcher.get_default_branch.return_value = _model_mock(
        {"id": "refs/heads/main", "display_id": "main", "type": "BRANCH"}
    )
    mock_fetcher.get_current_user_profile.return_value = _model_mock(
        {
            "name": "jdoe",
            "slug": "jdoe-slug",
            "id": 101,
            "display_name": "J. Doe",
            "active": True,
            "type": "NORMAL",
        }
    )

    tag = _model_mock(
        {"id": "refs/tags/v1.0.0", "display_id": "v1.0.0", "latest_commit": "def456"},
        {"display_id": "v1.0.0", "latest_commit": "def456"},
    )
    mock_fetcher.list_tags.return_value = BitbucketTagsPage(
        tags=[tag], is_last_page=True, truncated=False, next_page_start=None
    )
    mock_fetcher.get_tag.return_value = _model_mock(
        {"display_id": "v1.0.0", "latest_commit": "def456", "hash": "objsha"}
    )

    commit = _model_mock(
        {
            "id": "abc123def456",
            "display_id": "abc123d",
            "message": "Add feature\n\nbody",
            "author": {"name": "Jane Dev"},
        },
        {"display_id": "abc123d", "author": "Jane Dev", "message": "Add feature"},
    )
    commits_page = BitbucketCommitsPage(
        commits=[commit], is_last_page=True, truncated=False, next_page_start=None
    )
    mock_fetcher.list_commits.return_value = commits_page
    mock_fetcher.get_pull_request_commits.return_value = commits_page
    mock_fetcher.get_commit.return_value = commit
    mock_fetcher.compare_commits.return_value = commits_page

    build_status = _model_mock(
        {
            "key": "PLAN-UNIT",
            "state": "SUCCESSFUL",
            "name": "Unit tests",
            "url": "https://ci.example.com/browse/PLAN-UNIT-3",
            "test_results": {"successful": 10, "failed": 0, "skipped": 1},
        }
    )
    mock_fetcher.get_commit_build_statuses.return_value = BitbucketBuildStatusesPage(
        statuses=[build_status],
        is_last_page=True,
        truncated=False,
        next_page_start=None,
        page_counts={"SUCCESSFUL": 1},
    )

    directory_entry = _model_mock({"path": "app.py", "type": "FILE", "size": 12})
    mock_fetcher.browse.return_value = BitbucketBrowseResult(
        kind="DIRECTORY",
        path="src",
        lines=None,
        children=[directory_entry],
        is_last_page=True,
        next_page_start=None,
        truncated=False,
        binary=False,
    )

    pull_request = _model_mock(
        {"id": 5, "title": "Add X", "state": "OPEN", "reviewers": []},
        {"id": 5, "title": "Add X", "state": "OPEN"},
    )
    mock_fetcher.list_pull_requests.return_value = BitbucketPullRequestsPage(
        pull_requests=[pull_request],
        is_last_page=True,
        truncated=False,
        next_page_start=None,
    )
    dashboard_pull_request = _model_mock(
        {
            "id": 5,
            "title": "Add X",
            "state": "OPEN",
            "to_ref": {
                "display_id": "main",
                "project_key": "PROJ",
                "repository_slug": "my-repo",
            },
            "reviewers": [],
        },
        {
            "id": 5,
            "title": "Add X",
            "state": "OPEN",
            "project_key": "PROJ",
            "repository_slug": "my-repo",
        },
    )
    mock_fetcher.list_user_pull_requests.return_value = BitbucketPullRequestsPage(
        pull_requests=[dashboard_pull_request],
        is_last_page=True,
        truncated=False,
        next_page_start=None,
    )
    mock_fetcher.get_pull_request.return_value = pull_request
    mock_fetcher.get_pull_request_merge_status.return_value = _model_mock(
        {
            "can_merge": False,
            "conflicted": False,
            "outcome": "CLEAN",
            "vetoes": [{"summary": "Requires approvals", "detail": "Need 2."}],
        }
    )
    change = _model_mock({"path": "src/app.py", "type": "MODIFY", "node_type": "FILE"})
    changes_page = BitbucketChangesPage(
        changes=[change], is_last_page=True, truncated=False, next_page_start=None
    )
    mock_fetcher.get_pull_request_changes.return_value = changes_page
    mock_fetcher.compare_changes.return_value = changes_page
    diff = _model_mock(
        {"files": [{"path": "f.py"}], "count": 1, "total_files": 1, "truncated": False}
    )
    mock_fetcher.get_pull_request_diff.return_value = diff
    mock_fetcher.compare_diff.return_value = diff

    comment = _model_mock({"id": 9, "version": 1, "text": "nit", "author": "r"})
    activity = MagicMock()
    activity.to_simplified_dict.return_value = {"id": 1, "action": "COMMENTED"}
    activity.comment = comment
    mock_fetcher.get_activities.return_value = BitbucketActivitiesPage(
        activities=[activity], is_last_page=True, truncated=False, next_page_start=None
    )

    mock_fetcher.create_pull_request.return_value = _model_mock(
        {
            "id": 42,
            "title": "Add feature",
            "state": "OPEN",
            "from_ref": {"id": "refs/heads/feature/x"},
            "to_ref": {"id": "refs/heads/main"},
            "version": 0,
        }
    )
    mock_fetcher.add_comment.return_value = _model_mock(
        {"id": 101, "version": 0, "text": "hello"}
    )
    mock_fetcher.set_review_status.return_value = {
        "status": "APPROVED",
        "user": {"name": "me"},
    }
    mock_fetcher.update_comment.return_value = _model_mock(
        {"id": 9, "version": 4, "text": "edited", "thread_resolved": True}
    )
    mock_fetcher.set_task_state.return_value = _model_mock(
        {"id": 9, "version": 4, "severity": "BLOCKER", "state": "RESOLVED"}
    )
    mock_fetcher.delete_comment.return_value = None
    mock_fetcher.merge_pull_request.return_value = _model_mock(
        {"id": 5, "version": 4, "state": "MERGED", "title": "Add X"}
    )
    mock_fetcher.decline_pull_request.return_value = _model_mock(
        {"id": 5, "version": 4, "state": "DECLINED", "title": "Add X"}
    )
    mock_fetcher.reopen_pull_request.return_value = _model_mock(
        {"id": 5, "version": 4, "state": "OPEN", "title": "Add X"}
    )

    mock_config = MagicMock()
    mock_config.url = BASE_URL
    mock_fetcher.config = mock_config

    return mock_fetcher


@pytest.fixture
def mock_base_bitbucket_config() -> BitbucketConfig:
    """Create a base BitbucketConfig for MainAppContext using OAuth."""
    oauth_config = OAuthConfig(
        client_id="server_client_id",
        client_secret="server_client_secret",
        redirect_uri="http://localhost",
        scope="REPO_READ",
        base_url=BASE_URL,
    )
    return BitbucketConfig(
        url=BASE_URL,
        auth_type="oauth",
        oauth_config=oauth_config,
    )


@pytest.fixture
def test_bitbucket_mcp(
    mock_bitbucket_fetcher: MagicMock,
    mock_base_bitbucket_config: BitbucketConfig,
) -> AtlassianMCP:
    """Create a test FastMCP instance with standard configuration."""

    # Import and register tool functions (as they are in bitbucket.py)
    from src.mcp_atlassian.servers.bitbucket import (
        add_pull_request_comment,
        browse_path,
        compare_changes,
        compare_commits,
        compare_diff,
        create_pull_request,
        decline_pull_request,
        delete_pull_request_comment,
        edit_pull_request_comment,
        get_commit,
        get_commit_build_status,
        get_current_user,
        get_default_branch,
        get_pull_request,
        get_pull_request_activities,
        get_pull_request_changes,
        get_pull_request_comments,
        get_pull_request_commits,
        get_pull_request_diff,
        get_pull_request_merge_status,
        get_tag,
        list_branches,
        list_commits,
        list_projects,
        list_pull_requests,
        list_repositories,
        list_tags,
        list_user_pull_requests,
        merge_pull_request,
        reopen_pull_request,
        resolve_pull_request_comment,
        resolve_pull_request_task,
        set_pull_request_review_status,
    )

    @asynccontextmanager
    async def test_lifespan(app: FastMCP) -> AsyncGenerator[MainAppContext, None]:
        try:
            yield MainAppContext(
                full_bitbucket_config=mock_base_bitbucket_config, read_only=False
            )
        finally:
            pass

    test_mcp = AtlassianMCP(
        "TestBitbucket",
        instructions="Test Bitbucket MCP Server",
        lifespan=test_lifespan,
    )

    bitbucket_sub_mcp = FastMCP(name="TestBitbucketSubMCP")
    bitbucket_sub_mcp.add_tool(list_projects)
    bitbucket_sub_mcp.add_tool(list_repositories)
    bitbucket_sub_mcp.add_tool(list_branches)
    bitbucket_sub_mcp.add_tool(list_tags)
    bitbucket_sub_mcp.add_tool(get_tag)
    bitbucket_sub_mcp.add_tool(get_default_branch)
    bitbucket_sub_mcp.add_tool(list_commits)
    bitbucket_sub_mcp.add_tool(get_commit)
    bitbucket_sub_mcp.add_tool(get_commit_build_status)
    bitbucket_sub_mcp.add_tool(browse_path)
    bitbucket_sub_mcp.add_tool(compare_changes)
    bitbucket_sub_mcp.add_tool(compare_commits)
    bitbucket_sub_mcp.add_tool(compare_diff)
    bitbucket_sub_mcp.add_tool(list_pull_requests)
    bitbucket_sub_mcp.add_tool(list_user_pull_requests)
    bitbucket_sub_mcp.add_tool(get_pull_request)
    bitbucket_sub_mcp.add_tool(get_pull_request_commits)
    bitbucket_sub_mcp.add_tool(get_pull_request_merge_status)
    bitbucket_sub_mcp.add_tool(get_pull_request_changes)
    bitbucket_sub_mcp.add_tool(get_pull_request_diff)
    bitbucket_sub_mcp.add_tool(get_pull_request_activities)
    bitbucket_sub_mcp.add_tool(get_pull_request_comments)
    bitbucket_sub_mcp.add_tool(create_pull_request)
    bitbucket_sub_mcp.add_tool(add_pull_request_comment)
    bitbucket_sub_mcp.add_tool(set_pull_request_review_status)
    bitbucket_sub_mcp.add_tool(edit_pull_request_comment)
    bitbucket_sub_mcp.add_tool(resolve_pull_request_comment)
    bitbucket_sub_mcp.add_tool(resolve_pull_request_task)
    bitbucket_sub_mcp.add_tool(delete_pull_request_comment)
    bitbucket_sub_mcp.add_tool(merge_pull_request)
    bitbucket_sub_mcp.add_tool(decline_pull_request)
    bitbucket_sub_mcp.add_tool(reopen_pull_request)
    bitbucket_sub_mcp.add_tool(get_current_user)

    test_mcp.mount(bitbucket_sub_mcp, namespace="bitbucket")

    return test_mcp


@pytest.fixture
async def bitbucket_client(
    test_bitbucket_mcp: AtlassianMCP, mock_bitbucket_fetcher: MagicMock
) -> AsyncGenerator[Client, None]:
    """Create a FastMCP client with a mocked Bitbucket fetcher."""
    with (
        patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            AsyncMock(return_value=mock_bitbucket_fetcher),
        ),
        patch(
            "src.mcp_atlassian.servers.dependencies.get_http_request",
            MagicMock(spec=Request, state=MagicMock()),
        ),
    ):
        client_instance = Client(transport=FastMCPTransport(test_bitbucket_mcp))
        async with client_instance as connected_client:
            yield connected_client


def _read_only_context() -> MagicMock:
    """Build a tool context whose lifespan reports read-only mode."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "app_lifespan_context": MainAppContext(read_only=True)
    }
    return ctx


def _result_json(response) -> dict:
    """Decode a tool response's JSON payload."""
    return json.loads(response.content[0].text)


@pytest.mark.anyio
class TestListProjects:
    """The list_projects tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns projects plus pagination fields."""
        response = await bitbucket_client.call_tool("bitbucket_list_projects", {})

        mock_bitbucket_fetcher.list_projects.assert_called_once_with(
            name=None, start=0, limit=25
        )
        result = _result_json(response)
        assert result["projects"] == [
            {"key": "PROJ", "name": "Project", "description": "A project"}
        ]
        assert result["count"] == 1
        assert result["is_last_page"] is True
        assert result["truncated"] is False
        assert result["next_page_start"] is None

    async def test_filter_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The name filter and pagination cursor thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_list_projects", {"name": "Proj", "start": 10, "limit": 5}
        )

        mock_bitbucket_fetcher.list_projects.assert_called_once_with(
            name="Proj", start=10, limit=5
        )

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        """summary=True returns identity fields only, via to_summary_dict."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_projects", {"summary": True}
        )

        result = _result_json(response)
        assert result["projects"] == [{"key": "PROJ", "name": "Project"}]
        # The projection is applied in the tool; the fetcher call is unchanged.
        assert "summary" not in mock_bitbucket_fetcher.list_projects.call_args.kwargs

    async def test_empty_result(self, bitbucket_client, mock_bitbucket_fetcher):
        """An empty page yields an empty list and count 0."""
        mock_bitbucket_fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[], is_last_page=True, truncated=False, next_page_start=None
        )

        response = await bitbucket_client.call_tool("bitbucket_list_projects", {})

        result = _result_json(response)
        assert result["projects"] == []
        assert result["count"] == 0

    async def test_truncated_page_passthrough(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """truncated and next_page_start pass through from the fetcher page."""
        mock_bitbucket_fetcher.list_projects.return_value = BitbucketProjectsPage(
            projects=[], is_last_page=False, truncated=True, next_page_start=25
        )

        response = await bitbucket_client.call_tool("bitbucket_list_projects", {})

        result = _result_json(response)
        assert result["is_last_page"] is False
        assert result["truncated"] is True
        assert result["next_page_start"] == 25

    async def test_auth_error_preserved(self, bitbucket_client, mock_bitbucket_fetcher):
        """An authentication error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.list_projects.side_effect = (
            MCPAtlassianAuthenticationError("token rejected")
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool("bitbucket_list_projects", {})

        assert "Error calling tool 'list_projects'" in str(excinfo.value)
        assert "token rejected" in str(excinfo.value)

    async def test_not_found_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A not-found error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.list_projects.side_effect = (
            BitbucketResourceNotFoundError("Bitbucket resource not found (HTTP 404)")
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool("bitbucket_list_projects", {})

        assert "Bitbucket resource not found (HTTP 404)" in str(excinfo.value)

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A plain ValueError surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.list_projects.side_effect = ValueError(
            "Bitbucket API request failed with HTTP 500."
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool("bitbucket_list_projects", {})

        assert "Bitbucket API request failed with HTTP 500." in str(excinfo.value)


@pytest.mark.anyio
class TestListRepositories:
    """The list_repositories tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns repositories plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_repositories", {"project_key": "PROJ"}
        )

        mock_bitbucket_fetcher.list_repositories.assert_called_once_with(
            project_key="PROJ", name=None, start=0, limit=25
        )
        result = _result_json(response)
        assert result["repositories"] == [
            {"slug": "my-repo", "name": "my-repo", "project": {"key": "PROJ"}}
        ]
        assert result["count"] == 1
        assert result["is_last_page"] is True

    async def test_name_filter_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The repository-name filter and cursor thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_list_repositories",
            {"project_key": "PROJ", "name": "api", "start": 25, "limit": 10},
        )

        mock_bitbucket_fetcher.list_repositories.assert_called_once_with(
            project_key="PROJ", name="api", start=25, limit=10
        )

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        """summary=True returns identity fields only."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_repositories", {"project_key": "PROJ", "summary": True}
        )

        result = _result_json(response)
        assert result["repositories"] == [{"slug": "my-repo", "name": "my-repo"}]

    async def test_empty_result(self, bitbucket_client, mock_bitbucket_fetcher):
        """An empty page yields an empty list and count 0."""
        mock_bitbucket_fetcher.list_repositories.return_value = (
            BitbucketRepositoriesPage(
                repositories=[],
                is_last_page=True,
                truncated=False,
                next_page_start=None,
            )
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_list_repositories", {"project_key": "PROJ"}
        )

        result = _result_json(response)
        assert result["repositories"] == []
        assert result["count"] == 0


@pytest.mark.anyio
class TestListBranches:
    """The list_branches tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns branches plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_branches",
            {"project_key": "PROJ", "repository_slug": "my-repo"},
        )

        result = _result_json(response)
        assert result["branches"] == [
            {
                "id": "refs/heads/main",
                "display_id": "main",
                "latest_commit": "abc123",
            }
        ]
        assert result["count"] == 1

    async def test_filters_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """All branch filters and the cursor thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_list_branches",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "filter_text": "main",
                "order_by": "MODIFICATION",
                "boost_matches": True,
                "start": 10,
                "limit": 50,
            },
        )

        mock_bitbucket_fetcher.list_branches.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            filter_text="main",
            order_by="MODIFICATION",
            boost_matches=True,
            start=10,
            limit=50,
        )

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        """summary=True returns triage fields only."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_branches",
            {"project_key": "PROJ", "repository_slug": "my-repo", "summary": True},
        )

        result = _result_json(response)
        assert result["branches"] == [{"display_id": "main", "latest_commit": "abc123"}]


@pytest.mark.anyio
class TestListTags:
    """The list_tags tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns tags plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_tags",
            {"project_key": "PROJ", "repository_slug": "my-repo"},
        )

        result = _result_json(response)
        assert result["tags"] == [
            {
                "id": "refs/tags/v1.0.0",
                "display_id": "v1.0.0",
                "latest_commit": "def456",
            }
        ]
        assert result["count"] == 1

    async def test_filters_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The tag filters and cursor thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_list_tags",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "filter_text": "v1",
                "order_by": "ALPHABETICAL",
                "start": 10,
                "limit": 50,
            },
        )

        mock_bitbucket_fetcher.list_tags.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            filter_text="v1",
            order_by="ALPHABETICAL",
            start=10,
            limit=50,
        )

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        """summary=True returns triage fields only."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_tags",
            {"project_key": "PROJ", "repository_slug": "my-repo", "summary": True},
        )

        result = _result_json(response)
        assert result["tags"] == [{"display_id": "v1.0.0", "latest_commit": "def456"}]


@pytest.mark.anyio
class TestGetTag:
    """The get_tag tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the tag entity directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_tag",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "name": "v1.0.0",
            },
        )

        mock_bitbucket_fetcher.get_tag.assert_called_once_with(
            project_key="PROJ", repository_slug="my-repo", name="v1.0.0"
        )
        result = _result_json(response)
        assert result == {
            "display_id": "v1.0.0",
            "latest_commit": "def456",
            "hash": "objsha",
        }


@pytest.mark.anyio
class TestGetDefaultBranch:
    """The get_default_branch tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the branch entity directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_default_branch",
            {"project_key": "PROJ", "repository_slug": "my-repo"},
        )

        mock_bitbucket_fetcher.get_default_branch.assert_called_once_with(
            project_key="PROJ", repository_slug="my-repo"
        )
        result = _result_json(response)
        assert result == {
            "id": "refs/heads/main",
            "display_id": "main",
            "type": "BRANCH",
        }


@pytest.mark.anyio
class TestListCommits:
    """The list_commits tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns commits plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_commits",
            {"project_key": "PROJ", "repository_slug": "my-repo"},
        )

        result = _result_json(response)
        assert result["count"] == 1
        assert result["commits"][0]["display_id"] == "abc123d"
        assert result["is_last_page"] is True

    async def test_filters_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """All history filters and the cursor thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_list_commits",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "since": "oldsha",
                "until": "main",
                "path": "src/app.py",
                "merges": "only",
                "follow_renames": True,
                "ignore_missing": False,
                "start": 10,
                "limit": 50,
            },
        )

        mock_bitbucket_fetcher.list_commits.assert_called_once_with(
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

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        """summary=True returns triage fields only."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_commits",
            {"project_key": "PROJ", "repository_slug": "my-repo", "summary": True},
        )

        result = _result_json(response)
        assert result["commits"] == [
            {"display_id": "abc123d", "author": "Jane Dev", "message": "Add feature"}
        ]

    async def test_empty_result(self, bitbucket_client, mock_bitbucket_fetcher):
        """An empty page yields an empty list and count 0."""
        mock_bitbucket_fetcher.list_commits.return_value = BitbucketCommitsPage(
            commits=[], is_last_page=True, truncated=False, next_page_start=None
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_list_commits",
            {"project_key": "PROJ", "repository_slug": "my-repo"},
        )

        result = _result_json(response)
        assert result["commits"] == []
        assert result["count"] == 0


@pytest.mark.anyio
class TestGetCommit:
    """The get_commit tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the commit entity directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_commit",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "commit_id": "abc123def456",
            },
        )

        mock_bitbucket_fetcher.get_commit.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            commit_id="abc123def456",
        )
        result = _result_json(response)
        assert result["id"] == "abc123def456"
        assert result["display_id"] == "abc123d"


@pytest.mark.anyio
class TestGetCommitBuildStatus:
    """The get_commit_build_status tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the statuses with the page envelope."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_commit_build_status",
            {"commit_id": FULL_SHA.upper()},
        )

        mock_bitbucket_fetcher.get_commit_build_statuses.assert_called_once_with(
            commit_id=FULL_SHA.upper(),
            order_by=None,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        # The echoed id is normalised to the lower-case form the fetcher sends.
        assert result["commit_id"] == FULL_SHA
        assert result["count"] == 1
        assert result["build_statuses"][0]["state"] == "SUCCESSFUL"
        assert result["build_statuses"][0]["test_results"]["successful"] == 10
        assert result["page_counts"] == {"SUCCESSFUL": 1}
        assert result["is_last_page"] is True
        assert result["truncated"] is False
        assert result["next_page_start"] is None

    async def test_order_by_and_paging_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """order_by, start, and limit reach the fetcher unchanged."""
        await bitbucket_client.call_tool(
            "bitbucket_get_commit_build_status",
            {"commit_id": FULL_SHA, "order_by": "STATUS", "start": 25, "limit": 50},
        )

        mock_bitbucket_fetcher.get_commit_build_statuses.assert_called_once_with(
            commit_id=FULL_SHA, order_by="STATUS", start=25, limit=50
        )

    async def test_limit_above_ceiling_is_rejected(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A limit over the endpoint's 100-status cap fails schema validation."""
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_get_commit_build_status",
                {"commit_id": FULL_SHA, "limit": 101},
            )
        mock_bitbucket_fetcher.get_commit_build_statuses.assert_not_called()

    async def test_invalid_commit_id_surfaces_value_error(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A fetcher ValueError reaches the client as a tool error."""
        mock_bitbucket_fetcher.get_commit_build_statuses.side_effect = ValueError(
            "commit_id must be a full commit SHA of 40 hexadecimal characters."
        )
        with pytest.raises(ToolError, match="commit_id must be a full commit SHA"):
            await bitbucket_client.call_tool(
                "bitbucket_get_commit_build_status", {"commit_id": "not-a-sha"}
            )


@pytest.mark.anyio
class TestBrowsePath:
    """The browse_path tool's directory and file response shapes."""

    async def test_directory_shape(self, bitbucket_client, mock_bitbucket_fetcher):
        """A directory result carries children/count and no file fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_browse_path",
            {"project_key": "PROJ", "repository_slug": "my-repo", "path": "src"},
        )

        mock_bitbucket_fetcher.browse.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            path="src",
            at=None,
            start=0,
            limit=100,
        )
        result = _result_json(response)
        assert result["type"] == "DIRECTORY"
        assert result["path"] == "src"
        assert result["children"] == [{"path": "app.py", "type": "FILE", "size": 12}]
        assert result["count"] == 1
        assert "lines" not in result
        assert "binary" not in result

    async def test_file_shape(self, bitbucket_client, mock_bitbucket_fetcher):
        """A file result carries lines/binary/count and no children."""
        mock_bitbucket_fetcher.browse.return_value = BitbucketBrowseResult(
            kind="FILE",
            path="src/app.py",
            lines=["import os", "print(1)"],
            children=None,
            is_last_page=True,
            next_page_start=None,
            truncated=False,
            binary=False,
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_browse_path",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "path": "src/app.py",
            },
        )

        result = _result_json(response)
        assert result["type"] == "FILE"
        assert result["lines"] == ["import os", "print(1)"]
        assert result["binary"] is False
        assert result["count"] == 2
        assert "children" not in result

    async def test_binary_file_flag(self, bitbucket_client, mock_bitbucket_fetcher):
        """A binary file surfaces binary=true with no text lines."""
        mock_bitbucket_fetcher.browse.return_value = BitbucketBrowseResult(
            kind="FILE",
            path="img.png",
            lines=[],
            children=None,
            is_last_page=True,
            next_page_start=None,
            truncated=False,
            binary=True,
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_browse_path",
            {"project_key": "PROJ", "repository_slug": "my-repo", "path": "img.png"},
        )

        result = _result_json(response)
        assert result["type"] == "FILE"
        assert result["binary"] is True
        assert result["lines"] == []

    async def test_cursor_and_pagination_passthrough(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The cursor threads through and pagination fields pass through."""
        mock_bitbucket_fetcher.browse.return_value = BitbucketBrowseResult(
            kind="FILE",
            path="big.txt",
            lines=["a"],
            children=None,
            is_last_page=False,
            next_page_start=100,
            truncated=True,
            binary=False,
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_browse_path",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "path": "big.txt",
                "at": "main",
                "start": 50,
                "limit": 50,
            },
        )

        call_kwargs = mock_bitbucket_fetcher.browse.call_args.kwargs
        assert call_kwargs["at"] == "main"
        assert call_kwargs["start"] == 50
        assert call_kwargs["limit"] == 50
        result = _result_json(response)
        assert result["is_last_page"] is False
        assert result["truncated"] is True
        assert result["next_page_start"] == 100

    async def test_auth_error_preserved(self, bitbucket_client, mock_bitbucket_fetcher):
        """An authentication error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.browse.side_effect = MCPAtlassianAuthenticationError(
            "session expired"
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_browse_path",
                {"project_key": "PROJ", "repository_slug": "my-repo"},
            )

        assert "session expired" in str(excinfo.value)

    async def test_not_found_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A not-found error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.browse.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for the browse path."
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_browse_path",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "path": "missing",
                },
            )

        assert "Bitbucket resource not found (HTTP 404)" in str(excinfo.value)

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A path-validation ValueError surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.browse.side_effect = ValueError(
            "path traversal rejected"
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_browse_path",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "path": "a/b",
                },
            )

        assert "path traversal rejected" in str(excinfo.value)


@pytest.mark.anyio
class TestCompareChanges:
    """The compare_changes tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the changed files plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_compare_changes",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "from_ref": "feature/x",
            },
        )

        mock_bitbucket_fetcher.compare_changes.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            from_ref="feature/x",
            to_ref=None,
            from_repo=None,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["changes"] == [
            {"path": "src/app.py", "type": "MODIFY", "node_type": "FILE"}
        ]
        assert result["count"] == 1
        assert result["is_last_page"] is True
        assert result["truncated"] is False
        assert result["next_page_start"] is None

    async def test_refs_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        mock_bitbucket_fetcher.compare_changes.return_value = BitbucketChangesPage(
            changes=[], is_last_page=False, truncated=True, next_page_start=50
        )
        response = await bitbucket_client.call_tool(
            "bitbucket_compare_changes",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "from_ref": "feature/x",
                "to_ref": "main",
                "from_repo": "FORK/my-repo",
                "start": 25,
                "limit": 25,
            },
        )

        call_kwargs = mock_bitbucket_fetcher.compare_changes.call_args.kwargs
        assert call_kwargs["to_ref"] == "main"
        assert call_kwargs["from_repo"] == "FORK/my-repo"
        assert call_kwargs["start"] == 25
        result = _result_json(response)
        assert result["next_page_start"] == 50
        assert result["truncated"] is True

    async def test_from_ref_is_required(self, bitbucket_client, mock_bitbucket_fetcher):
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_compare_changes",
                {"project_key": "PROJ", "repository_slug": "my-repo"},
            )
        mock_bitbucket_fetcher.compare_changes.assert_not_called()

    async def test_limit_above_ceiling_rejected(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_compare_changes",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "from_ref": "x",
                    "limit": 101,
                },
            )
        mock_bitbucket_fetcher.compare_changes.assert_not_called()

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A malformed from_repo surfaces as the fetcher's message."""
        mock_bitbucket_fetcher.compare_changes.side_effect = ValueError(
            "from_repo must be a project key and repository slug"
        )
        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_compare_changes",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "from_ref": "x",
                    "from_repo": "42",
                },
            )
        assert "from_repo" in str(excinfo.value)


@pytest.mark.anyio
class TestCompareCommits:
    """The compare_commits tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        response = await bitbucket_client.call_tool(
            "bitbucket_compare_commits",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "from_ref": "release/1.2",
                "to_ref": "refs/tags/v1.1",
            },
        )

        mock_bitbucket_fetcher.compare_commits.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            from_ref="release/1.2",
            to_ref="refs/tags/v1.1",
            from_repo=None,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["commits"][0]["id"] == "abc123def456"
        assert result["commits"][0]["message"] == "Add feature\n\nbody"
        assert result["count"] == 1
        assert result["is_last_page"] is True
        assert result["next_page_start"] is None

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        response = await bitbucket_client.call_tool(
            "bitbucket_compare_commits",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "from_ref": "x",
                "summary": True,
            },
        )

        result = _result_json(response)
        assert result["commits"] == [
            {"display_id": "abc123d", "author": "Jane Dev", "message": "Add feature"}
        ]

    async def test_not_found_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        mock_bitbucket_fetcher.compare_commits.side_effect = (
            BitbucketResourceNotFoundError("Bitbucket resource not found (HTTP 404)")
        )
        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_compare_commits",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "from_ref": "no-such-ref",
                },
            )
        assert "not found" in str(excinfo.value)


@pytest.mark.anyio
class TestCompareDiff:
    """The compare_diff tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        response = await bitbucket_client.call_tool(
            "bitbucket_compare_diff",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "from_ref": "feature/x",
            },
        )

        mock_bitbucket_fetcher.compare_diff.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            from_ref="feature/x",
            to_ref=None,
            from_repo=None,
            max_lines_per_file=500,
            max_files=100,
            context_lines=None,
            path=None,
            src_path=None,
            whitespace=None,
        )
        result = _result_json(response)
        assert result["files"] == [{"path": "f.py"}]
        assert result["truncated"] is False

    async def test_narrowing_is_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        await bitbucket_client.call_tool(
            "bitbucket_compare_diff",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "from_ref": "feature/x",
                "to_ref": "main",
                "from_repo": "FORK/my-repo",
                "max_lines_per_file": 50,
                "max_files": 5,
                "context_lines": 2,
                "path": "src/app.py",
                "src_path": "src/old.py",
                "whitespace": "ignore-all",
            },
        )

        call_kwargs = mock_bitbucket_fetcher.compare_diff.call_args.kwargs
        assert call_kwargs["to_ref"] == "main"
        assert call_kwargs["from_repo"] == "FORK/my-repo"
        assert call_kwargs["max_lines_per_file"] == 50
        assert call_kwargs["max_files"] == 5
        assert call_kwargs["context_lines"] == 2
        assert call_kwargs["path"] == "src/app.py"
        assert call_kwargs["src_path"] == "src/old.py"
        assert call_kwargs["whitespace"] == "ignore-all"

    async def test_context_lines_above_ceiling_rejected(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_compare_diff",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "from_ref": "x",
                    "context_lines": 1001,
                },
            )
        mock_bitbucket_fetcher.compare_diff.assert_not_called()

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """An unrecognised whitespace value surfaces as the fetcher's message."""
        mock_bitbucket_fetcher.compare_diff.side_effect = ValueError(
            "whitespace must be one of ignore-all."
        )
        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_compare_diff",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "from_ref": "x",
                    "whitespace": "ignore-some",
                },
            )
        assert "whitespace must be one of" in str(excinfo.value)


@pytest.mark.anyio
class TestListPullRequests:
    """The list_pull_requests tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns pull requests plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_pull_requests",
            {"project_key": "PROJ", "repository_slug": "my-repo"},
        )

        mock_bitbucket_fetcher.list_pull_requests.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            state=None,
            direction=None,
            at=None,
            order=None,
            filter_text=None,
            draft=None,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["pull_requests"] == [
            {"id": 5, "title": "Add X", "state": "OPEN", "reviewers": []}
        ]
        assert result["count"] == 1

    async def test_filters_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """All PR filters and the cursor thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_list_pull_requests",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "state": "MERGED",
                "direction": "OUTGOING",
                "at": "refs/heads/main",
                "order": "OLDEST",
                "filter_text": "login",
                "draft": True,
                "start": 10,
                "limit": 50,
            },
        )

        mock_bitbucket_fetcher.list_pull_requests.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            state="MERGED",
            direction="OUTGOING",
            at="refs/heads/main",
            order="OLDEST",
            filter_text="login",
            draft=True,
            start=10,
            limit=50,
        )

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        """summary=True returns triage fields only."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_pull_requests",
            {"project_key": "PROJ", "repository_slug": "my-repo", "summary": True},
        )

        result = _result_json(response)
        assert result["pull_requests"] == [{"id": 5, "title": "Add X", "state": "OPEN"}]

    async def test_empty_result(self, bitbucket_client, mock_bitbucket_fetcher):
        """An empty page yields an empty list and count 0."""
        mock_bitbucket_fetcher.list_pull_requests.return_value = (
            BitbucketPullRequestsPage(
                pull_requests=[],
                is_last_page=True,
                truncated=False,
                next_page_start=None,
            )
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_list_pull_requests",
            {"project_key": "PROJ", "repository_slug": "my-repo"},
        )

        result = _result_json(response)
        assert result["pull_requests"] == []
        assert result["count"] == 0


@pytest.mark.anyio
class TestListUserPullRequests:
    """The list_user_pull_requests tool."""

    async def test_success_defaults_to_the_caller(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A no-argument call lists the caller's pull requests."""
        response = await bitbucket_client.call_tool(
            "bitbucket_list_user_pull_requests", {}
        )

        mock_bitbucket_fetcher.list_user_pull_requests.assert_called_once_with(
            user=None,
            role=None,
            participant_status=None,
            state=None,
            order=None,
            closed_since=None,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["pull_requests"] == [
            {
                "id": 5,
                "title": "Add X",
                "state": "OPEN",
                "to_ref": {
                    "display_id": "main",
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                },
                "reviewers": [],
            }
        ]
        assert result["count"] == 1
        assert result["is_last_page"] is True
        assert result["truncated"] is False
        assert result["next_page_start"] is None

    async def test_filters_and_cursor_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """Every filter, the user, and the cursor thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_list_user_pull_requests",
            {
                "user": "alice",
                "role": "REVIEWER",
                "participant_status": ["UNAPPROVED", "NEEDS_WORK"],
                "state": "OPEN",
                "order": "PARTICIPANT_STATUS",
                "closed_since": 86400,
                "start": 10,
                "limit": 50,
            },
        )

        mock_bitbucket_fetcher.list_user_pull_requests.assert_called_once_with(
            user="alice",
            role="REVIEWER",
            participant_status=["UNAPPROVED", "NEEDS_WORK"],
            state="OPEN",
            order="PARTICIPANT_STATUS",
            closed_since=86400,
            start=10,
            limit=50,
        )

    async def test_summary_uses_triage_fields(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        response = await bitbucket_client.call_tool(
            "bitbucket_list_user_pull_requests", {"summary": True}
        )

        result = _result_json(response)
        assert result["pull_requests"] == [
            {
                "id": 5,
                "title": "Add X",
                "state": "OPEN",
                "project_key": "PROJ",
                "repository_slug": "my-repo",
            }
        ]

    async def test_non_positive_closed_since_is_rejected(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_list_user_pull_requests", {"closed_since": 0}
            )
        mock_bitbucket_fetcher.list_user_pull_requests.assert_not_called()

    async def test_fetcher_value_error_is_surfaced(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """An unrecognised enum raised by the fetcher reaches the client."""
        mock_bitbucket_fetcher.list_user_pull_requests.side_effect = ValueError(
            "role must be one of REVIEWER, AUTHOR, PARTICIPANT."
        )
        with pytest.raises(ToolError, match="role must be one of"):
            await bitbucket_client.call_tool(
                "bitbucket_list_user_pull_requests", {"role": "OWNER"}
            )


@pytest.mark.anyio
class TestGetPullRequest:
    """The get_pull_request tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the pull-request entity directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
            },
        )

        mock_bitbucket_fetcher.get_pull_request.assert_called_once_with(
            project_key="PROJ", repository_slug="my-repo", pull_request_id=5
        )
        result = _result_json(response)
        assert result["id"] == 5
        assert result["title"] == "Add X"

    async def test_auth_error_preserved(self, bitbucket_client, mock_bitbucket_fetcher):
        """An authentication error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.get_pull_request.side_effect = (
            MCPAtlassianAuthenticationError("token rejected")
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                },
            )

        assert "token rejected" in str(excinfo.value)

    async def test_not_found_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A not-found error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.get_pull_request.side_effect = (
            BitbucketResourceNotFoundError(
                "Bitbucket resource not found (HTTP 404) for the pull request."
            )
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 999,
                },
            )

        assert "Bitbucket resource not found (HTTP 404)" in str(excinfo.value)

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A plain ValueError surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.get_pull_request.side_effect = ValueError(
            "Bitbucket API request failed with HTTP 500."
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                },
            )

        assert "Bitbucket API request failed with HTTP 500." in str(excinfo.value)


@pytest.mark.anyio
class TestGetPullRequestMergeStatus:
    """The get_pull_request_merge_status tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the mergeability entity directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_merge_status",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
            },
        )

        mock_bitbucket_fetcher.get_pull_request_merge_status.assert_called_once_with(
            project_key="PROJ", repository_slug="my-repo", pull_request_id=5
        )
        result = _result_json(response)
        assert result["can_merge"] is False
        assert result["outcome"] == "CLEAN"
        assert result["vetoes"][0]["summary"] == "Requires approvals"

    async def test_invalid_id_rejected_before_call(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A non-positive id fails schema validation without reaching the fetcher."""
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request_merge_status",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 0,
                },
            )
        mock_bitbucket_fetcher.get_pull_request_merge_status.assert_not_called()

    async def test_not_open_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The 409 message for a closed pull request surfaces as a ToolError."""
        mock_bitbucket_fetcher.get_pull_request_merge_status.side_effect = ValueError(
            "Bitbucket API request to .../merge failed with HTTP 409: "
            "The pull request is not open."
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request_merge_status",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                },
            )

        assert "not open" in str(excinfo.value)

    async def test_not_found_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A not-found error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.get_pull_request_merge_status.side_effect = (
            BitbucketResourceNotFoundError(
                "Bitbucket resource not found (HTTP 404) for the pull request."
            )
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request_merge_status",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 999,
                },
            )

        assert "Bitbucket resource not found (HTTP 404)" in str(excinfo.value)


@pytest.mark.anyio
class TestGetPullRequestCommits:
    """The get_pull_request_commits tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the PR's commits plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_commits",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
            },
        )

        mock_bitbucket_fetcher.get_pull_request_commits.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["commits"][0]["display_id"] == "abc123d"
        assert result["count"] == 1

    async def test_cursor_is_forwarded(self, bitbucket_client, mock_bitbucket_fetcher):
        """The pagination cursor threads through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_commits",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "start": 10,
                "limit": 50,
            },
        )

        call_kwargs = mock_bitbucket_fetcher.get_pull_request_commits.call_args.kwargs
        assert call_kwargs["start"] == 10
        assert call_kwargs["limit"] == 50

    async def test_summary_projection(self, bitbucket_client, mock_bitbucket_fetcher):
        """summary=True returns triage fields only."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_commits",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "summary": True,
            },
        )

        result = _result_json(response)
        assert result["commits"] == [
            {"display_id": "abc123d", "author": "Jane Dev", "message": "Add feature"}
        ]


@pytest.mark.anyio
class TestGetPullRequestChanges:
    """The get_pull_request_changes tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the changed files plus pagination fields."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_changes",
            {"project_key": "PROJ", "repository_slug": "my-repo", "pull_request_id": 5},
        )

        mock_bitbucket_fetcher.get_pull_request_changes.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["changes"] == [
            {"path": "src/app.py", "type": "MODIFY", "node_type": "FILE"}
        ]
        assert result["count"] == 1
        assert result["is_last_page"] is True
        assert result["truncated"] is False
        assert result["next_page_start"] is None

    async def test_cursor_is_forwarded(self, bitbucket_client, mock_bitbucket_fetcher):
        """The pagination cursor threads through to the fetcher."""
        mock_bitbucket_fetcher.get_pull_request_changes.return_value = (
            BitbucketChangesPage(
                changes=[], is_last_page=False, truncated=True, next_page_start=50
            )
        )
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_changes",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "start": 25,
                "limit": 25,
            },
        )

        call_kwargs = mock_bitbucket_fetcher.get_pull_request_changes.call_args.kwargs
        assert call_kwargs["start"] == 25
        result = _result_json(response)
        assert result["next_page_start"] == 50
        assert result["truncated"] is True

    async def test_limit_above_ceiling_rejected(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request_changes",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "limit": 101,
                },
            )
        mock_bitbucket_fetcher.get_pull_request_changes.assert_not_called()


@pytest.mark.anyio
class TestGetPullRequestDiff:
    """The get_pull_request_diff tool."""

    async def test_success_and_caps_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A successful call returns the diff and forwards both size caps."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_diff",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "max_lines_per_file": 200,
                "max_files": 10,
            },
        )

        mock_bitbucket_fetcher.get_pull_request_diff.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            max_lines_per_file=200,
            max_files=10,
            context_lines=None,
            path=None,
            src_path=None,
        )
        result = _result_json(response)
        assert result["files"] == [{"path": "f.py"}]
        assert result["truncated"] is False

    async def test_narrowing_parameters_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """context_lines, path, and src_path thread through to the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_diff",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "context_lines": 3,
                "path": "src/app.py",
                "src_path": "src/old.py",
            },
        )

        call_kwargs = mock_bitbucket_fetcher.get_pull_request_diff.call_args.kwargs
        assert call_kwargs["context_lines"] == 3
        assert call_kwargs["path"] == "src/app.py"
        assert call_kwargs["src_path"] == "src/old.py"

    async def test_negative_context_lines_rejected(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The schema bound rejects a negative context_lines before the fetcher."""
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_get_pull_request_diff",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "context_lines": -1,
                },
            )
        mock_bitbucket_fetcher.get_pull_request_diff.assert_not_called()


@pytest.mark.anyio
class TestGetPullRequestActivities:
    """The get_pull_request_activities tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns activities without an action filter."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_activities",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
            },
        )

        mock_bitbucket_fetcher.get_activities.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["activities"] == [{"id": 1, "action": "COMMENTED"}]
        assert result["count"] == 1

    async def test_cursor_and_pagination_passthrough(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The cursor threads through and pagination fields pass through."""
        mock_bitbucket_fetcher.get_activities.return_value = BitbucketActivitiesPage(
            activities=[], is_last_page=False, truncated=True, next_page_start=25
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_activities",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "start": 10,
            },
        )

        assert mock_bitbucket_fetcher.get_activities.call_args.kwargs["start"] == 10
        result = _result_json(response)
        assert result["is_last_page"] is False
        assert result["truncated"] is True
        assert result["next_page_start"] == 25


@pytest.mark.anyio
class TestGetPullRequestComments:
    """The get_pull_request_comments tool."""

    async def test_filters_to_commented_and_extracts_comment(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The tool requests COMMENTED activities and surfaces their comments."""
        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_comments",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
            },
        )

        mock_bitbucket_fetcher.get_activities.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            action="COMMENTED",
            start=0,
            limit=25,
        )
        result = _result_json(response)
        assert result["comments"] == [
            {"id": 9, "version": 1, "text": "nit", "author": "r"}
        ]
        assert result["count"] == 1

    async def test_activities_without_comment_are_dropped(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """Activities whose comment payload is None are filtered out."""
        with_comment = MagicMock()
        with_comment.comment = _model_mock({"id": 9, "text": "x"})
        without_comment = MagicMock()
        without_comment.comment = None
        mock_bitbucket_fetcher.get_activities.return_value = BitbucketActivitiesPage(
            activities=[with_comment, without_comment],
            is_last_page=False,
            truncated=True,
            next_page_start=25,
        )

        response = await bitbucket_client.call_tool(
            "bitbucket_get_pull_request_comments",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
            },
        )

        result = _result_json(response)
        assert result["comments"] == [{"id": 9, "text": "x"}]
        # count is the number of comments returned.
        assert result["count"] == 1
        assert result["truncated"] is True
        assert result["is_last_page"] is False
        assert result["next_page_start"] == 25


@pytest.mark.anyio
class TestCreatePullRequest:
    """The create_pull_request write tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A minimal call forwards the required fields and returns the PR."""
        response = await bitbucket_client.call_tool(
            "bitbucket_create_pull_request",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "title": "Add feature",
                "from_ref": "feature/x",
                "to_ref": "main",
            },
        )

        mock_bitbucket_fetcher.create_pull_request.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            title="Add feature",
            from_ref="feature/x",
            to_ref="main",
            description=None,
            draft=None,
            reviewers=None,
            from_repo=None,
        )
        result = _result_json(response)
        assert result["id"] == 42
        assert result["from_ref"] == {"id": "refs/heads/feature/x"}
        assert result["version"] == 0

    async def test_optional_parameters_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        await bitbucket_client.call_tool(
            "bitbucket_create_pull_request",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "title": "Add feature",
                "from_ref": "refs/tags/v1",
                "to_ref": "main",
                "description": "body",
                "draft": True,
                "reviewers": ["alice", "bob"],
                "from_repo": "FORK/their-repo",
            },
        )
        call_kwargs = mock_bitbucket_fetcher.create_pull_request.call_args[1]
        assert call_kwargs["from_ref"] == "refs/tags/v1"
        assert call_kwargs["description"] == "body"
        assert call_kwargs["draft"] is True
        assert call_kwargs["reviewers"] == ["alice", "bob"]
        assert call_kwargs["from_repo"] == "FORK/their-repo"

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A validation or 409 rejection reaches the caller with its text."""
        mock_bitbucket_fetcher.create_pull_request.side_effect = ValueError(
            "Bitbucket request failed (HTTP 409): Only one pull request may be open"
        )
        with pytest.raises(ToolError, match="HTTP 409.*Only one pull request"):
            await bitbucket_client.call_tool(
                "bitbucket_create_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "title": "Add feature",
                    "from_ref": "feature/x",
                    "to_ref": "main",
                },
            )


@pytest.mark.anyio
class TestAddComment:
    """The add_pull_request_comment write tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the created comment entity directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_add_pull_request_comment",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "text": "hello",
            },
        )

        mock_bitbucket_fetcher.add_comment.assert_called_once_with(
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
        result = _result_json(response)
        assert result == {"id": 101, "version": 0, "text": "hello"}

    async def test_anchor_parameters_are_forwarded(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """File/line anchor and severity parameters thread through."""
        await bitbucket_client.call_tool(
            "bitbucket_add_pull_request_comment",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "text": "line note",
                "file_path": "src/app.py",
                "line": 10,
                "line_type": "ADDED",
                "file_type": "TO",
                "severity": "BLOCKER",
            },
        )

        call_kwargs = mock_bitbucket_fetcher.add_comment.call_args.kwargs
        assert call_kwargs["file_path"] == "src/app.py"
        assert call_kwargs["line"] == 10
        assert call_kwargs["line_type"] == "ADDED"
        assert call_kwargs["file_type"] == "TO"
        assert call_kwargs["severity"] == "BLOCKER"

    async def test_auth_error_preserved(self, bitbucket_client, mock_bitbucket_fetcher):
        """An authentication error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.add_comment.side_effect = (
            MCPAtlassianAuthenticationError("token rejected")
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_add_pull_request_comment",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "text": "x",
                },
            )

        assert "token rejected" in str(excinfo.value)

    async def test_not_found_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A not-found error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.add_comment.side_effect = BitbucketResourceNotFoundError(
            "Bitbucket resource not found (HTTP 404) for the pull request."
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_add_pull_request_comment",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 999,
                    "text": "x",
                },
            )

        assert "Bitbucket resource not found (HTTP 404)" in str(excinfo.value)

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A validation ValueError surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.add_comment.side_effect = ValueError(
            "text must be a non-blank string."
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_add_pull_request_comment",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "text": "  ",
                },
            )

        assert "text must be a non-blank string." in str(excinfo.value)


@pytest.mark.anyio
class TestSetReviewStatus:
    """The set_pull_request_review_status write tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the participant dict directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_set_pull_request_review_status",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "status": "APPROVED",
            },
        )

        mock_bitbucket_fetcher.set_review_status.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            status="APPROVED",
        )
        result = _result_json(response)
        assert result == {"status": "APPROVED", "user": {"name": "me"}}


@pytest.mark.anyio
class TestEditComment:
    """The edit_pull_request_comment write tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful edit returns the updated comment entity directly."""
        response = await bitbucket_client.call_tool(
            "bitbucket_edit_pull_request_comment",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "comment_id": 9,
                "text": "edited",
                "version": 3,
            },
        )

        mock_bitbucket_fetcher.update_comment.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            comment_id=9,
            version=3,
            text="edited",
        )
        result = _result_json(response)
        assert result["id"] == 9
        assert result["version"] == 4


@pytest.mark.anyio
class TestResolveComment:
    """The resolve_pull_request_comment write tool."""

    async def test_resolve_defaults_to_true(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The default resolves the thread (thread_resolved=True)."""
        response = await bitbucket_client.call_tool(
            "bitbucket_resolve_pull_request_comment",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "comment_id": 9,
                "version": 3,
            },
        )

        mock_bitbucket_fetcher.update_comment.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            comment_id=9,
            version=3,
            thread_resolved=True,
        )
        result = _result_json(response)
        assert result["thread_resolved"] is True

    async def test_reopen_passes_false(self, bitbucket_client, mock_bitbucket_fetcher):
        """resolved=False reopens the thread (thread_resolved=False)."""
        await bitbucket_client.call_tool(
            "bitbucket_resolve_pull_request_comment",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "comment_id": 9,
                "version": 3,
                "resolved": False,
            },
        )

        call_kwargs = mock_bitbucket_fetcher.update_comment.call_args.kwargs
        assert call_kwargs["thread_resolved"] is False


@pytest.mark.anyio
class TestResolveTask:
    """The resolve_pull_request_task write tool."""

    async def test_state_defaults_to_resolved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The default resolves the task (state=RESOLVED) and echoes the state."""
        response = await bitbucket_client.call_tool(
            "bitbucket_resolve_pull_request_task",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "comment_id": 9,
                "version": 3,
            },
        )

        mock_bitbucket_fetcher.set_task_state.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            comment_id=9,
            version=3,
            state="RESOLVED",
        )
        mock_bitbucket_fetcher.update_comment.assert_not_called()
        result = _result_json(response)
        assert result["state"] == "RESOLVED"
        assert result["severity"] == "BLOCKER"

    async def test_open_reopens_the_task(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """state=OPEN is passed through to reopen the task."""
        await bitbucket_client.call_tool(
            "bitbucket_resolve_pull_request_task",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "comment_id": 9,
                "version": 3,
                "state": "OPEN",
            },
        )

        call_kwargs = mock_bitbucket_fetcher.set_task_state.call_args.kwargs
        assert call_kwargs["state"] == "OPEN"

    async def test_unknown_state_surfaces_the_rejection(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The fetcher's rejection of an unknown state reaches the caller."""
        mock_bitbucket_fetcher.set_task_state.side_effect = ValueError(
            "state must be 'RESOLVED' or 'OPEN'."
        )
        with pytest.raises(ToolError, match="state must be 'RESOLVED' or 'OPEN'"):
            await bitbucket_client.call_tool(
                "bitbucket_resolve_pull_request_task",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "comment_id": 9,
                    "version": 3,
                    "state": "DONE",
                },
            )

    async def test_stale_version_surfaces_server_message(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A 409 stale-version message from the instance reaches the caller."""
        mock_bitbucket_fetcher.set_task_state.side_effect = ValueError(
            "Bitbucket API error (HTTP 409): The comment version is out of date."
        )
        with pytest.raises(ToolError, match="HTTP 409.*out of date"):
            await bitbucket_client.call_tool(
                "bitbucket_resolve_pull_request_task",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "comment_id": 9,
                    "version": 1,
                },
            )

    async def test_is_a_write_tool_in_the_pull_requests_toolset(self, bitbucket_client):
        """The write tag hides the tool under READ_ONLY_MODE, and the tool is
        flagged destructive.
        """
        from src.mcp_atlassian.servers.bitbucket import bitbucket_mcp

        tool = await bitbucket_mcp.get_tool("resolve_pull_request_task")
        assert "write" in tool.tags
        assert "read" not in tool.tags
        assert "toolset:bitbucket_pull_requests" in tool.tags
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is True

    @pytest.mark.parametrize("comment_id", [0, -1])
    async def test_rejects_non_positive_comment_id(
        self, bitbucket_client, mock_bitbucket_fetcher, comment_id
    ):
        """A non-positive comment id is rejected by the schema before any call."""
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_resolve_pull_request_task",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "comment_id": comment_id,
                    "version": 3,
                },
            )
        mock_bitbucket_fetcher.set_task_state.assert_not_called()

    async def test_rejects_negative_version(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A negative version is rejected by the schema. Zero is a fresh comment."""
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_resolve_pull_request_task",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "comment_id": 9,
                    "version": -1,
                },
            )
        mock_bitbucket_fetcher.set_task_state.assert_not_called()


@pytest.mark.anyio
class TestDeleteComment:
    """The delete_pull_request_comment write tool."""

    async def test_success_echoes_comment_id(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A successful delete (204, no body) confirms with the comment id."""
        response = await bitbucket_client.call_tool(
            "bitbucket_delete_pull_request_comment",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "comment_id": 9,
                "version": 3,
            },
        )

        mock_bitbucket_fetcher.delete_comment.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            comment_id=9,
            version=3,
        )
        result = _result_json(response)
        assert result == {"comment_id": 9}


@pytest.mark.anyio
class TestMergePullRequest:
    """The merge_pull_request write tool."""

    async def test_success_forwards_all_arguments(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A merge forwards version, message, and strategy_id verbatim."""
        response = await bitbucket_client.call_tool(
            "bitbucket_merge_pull_request",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "version": 3,
                "message": "Merge feature",
                "strategy_id": "squash-ff-only",
            },
        )

        mock_bitbucket_fetcher.merge_pull_request.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            version=3,
            message="Merge feature",
            strategy_id="squash-ff-only",
        )
        assert _result_json(response)["state"] == "MERGED"

    async def test_optionals_default_to_none(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        await bitbucket_client.call_tool(
            "bitbucket_merge_pull_request",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "version": 0,
            },
        )

        mock_bitbucket_fetcher.merge_pull_request.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            version=0,
            message=None,
            strategy_id=None,
        )

    async def test_missing_version_rejected_by_schema(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """The tool does not fetch the version, so the schema requires it."""
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_merge_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                },
            )
        mock_bitbucket_fetcher.merge_pull_request.assert_not_called()

    async def test_auto_merge_is_not_a_parameter(self, bitbucket_client):
        """autoMerge is not exposed, so the schema rejects it and it is not sent."""
        from src.mcp_atlassian.servers.bitbucket import bitbucket_mcp

        tool = await bitbucket_mcp.get_tool("merge_pull_request")
        assert "auto_merge" not in tool.parameters["properties"]
        assert "autoMerge" not in tool.parameters["properties"]
        assert "write" in tool.tags
        assert tool.annotations is not None
        assert tool.annotations.destructiveHint is True

    async def test_docstring_names_the_permission_and_the_prechecks(
        self, bitbucket_client
    ):
        """The generated docs carry the first line and parameter descriptions."""
        from src.mcp_atlassian.servers.bitbucket import bitbucket_mcp

        tool = await bitbucket_mcp.get_tool("merge_pull_request")
        first_line = (tool.description or "").strip().splitlines()[0]
        assert "REPO_WRITE" in first_line
        version_description = tool.parameters["properties"]["version"]["description"]
        assert "get_pull_request_merge_status" in version_description
        assert "get_pull_request" in version_description
        assert "REPO_READ" in (tool.description or "")

    async def test_veto_409_message_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        mock_bitbucket_fetcher.merge_pull_request.side_effect = ValueError(
            "Bitbucket API request to /x/merge failed with HTTP 409: "
            "Vetoed by a merge check."
        )
        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_merge_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "version": 3,
                },
            )
        assert "Vetoed by a merge check" in str(excinfo.value)

    async def test_auth_error_preserved(self, bitbucket_client, mock_bitbucket_fetcher):
        mock_bitbucket_fetcher.merge_pull_request.side_effect = (
            MCPAtlassianAuthenticationError(
                "Bitbucket authentication failed (HTTP 403). The forwarded OAuth "
                "bearer token was rejected or lacks permission for this resource: "
                "REPO_WRITE required."
            )
        )
        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_merge_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "version": 3,
                },
            )
        assert "REPO_WRITE required" in str(excinfo.value)


@pytest.mark.anyio
class TestDeclinePullRequest:
    """The decline_pull_request write tool."""

    async def test_success_forwards_comment(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        response = await bitbucket_client.call_tool(
            "bitbucket_decline_pull_request",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "version": 3,
                "comment": "Superseded by #7",
            },
        )

        mock_bitbucket_fetcher.decline_pull_request.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            version=3,
            comment="Superseded by #7",
        )
        assert _result_json(response)["state"] == "DECLINED"

    async def test_missing_version_rejected_by_schema(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_decline_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                },
            )
        mock_bitbucket_fetcher.decline_pull_request.assert_not_called()

    async def test_stale_version_message_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        mock_bitbucket_fetcher.decline_pull_request.side_effect = ValueError(
            "Bitbucket API request to /x/decline failed with HTTP 409: "
            "The pull request is not open."
        )
        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool(
                "bitbucket_decline_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                    "version": 1,
                },
            )
        assert "is not open" in str(excinfo.value)


@pytest.mark.anyio
class TestReopenPullRequest:
    """The reopen_pull_request write tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        response = await bitbucket_client.call_tool(
            "bitbucket_reopen_pull_request",
            {
                "project_key": "PROJ",
                "repository_slug": "my-repo",
                "pull_request_id": 5,
                "version": 3,
            },
        )

        mock_bitbucket_fetcher.reopen_pull_request.assert_called_once_with(
            project_key="PROJ",
            repository_slug="my-repo",
            pull_request_id=5,
            version=3,
        )
        assert _result_json(response)["state"] == "OPEN"

    async def test_missing_version_rejected_by_schema(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        with pytest.raises(ToolError):
            await bitbucket_client.call_tool(
                "bitbucket_reopen_pull_request",
                {
                    "project_key": "PROJ",
                    "repository_slug": "my-repo",
                    "pull_request_id": 5,
                },
            )
        mock_bitbucket_fetcher.reopen_pull_request.assert_not_called()

    async def test_is_a_write_tool_in_the_pull_requests_toolset(self, bitbucket_client):
        from src.mcp_atlassian.servers.bitbucket import bitbucket_mcp

        for name in (
            "merge_pull_request",
            "decline_pull_request",
            "reopen_pull_request",
        ):
            tool = await bitbucket_mcp.get_tool(name)
            assert "toolset:bitbucket_pull_requests" in tool.tags
            assert "write" in tool.tags


@pytest.mark.anyio
class TestReadOnlyMode:
    """All write tools are blocked before any fetcher call in read-only mode.

    The write gate runs on the tool function itself, so these tests invoke the
    registered tool functions directly with a read-only lifespan context (the
    same shape the real server lifespan yields).
    """

    async def test_create_pull_request_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import create_pull_request

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await create_pull_request(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    title="Add feature",
                    from_ref="feature/x",
                    to_ref="main",
                )
        fetcher_dependency.assert_not_called()

    async def test_add_pull_request_comment_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import add_pull_request_comment

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await add_pull_request_comment(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    text="x",
                )
        fetcher_dependency.assert_not_called()

    async def test_set_pull_request_review_status_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import set_pull_request_review_status

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await set_pull_request_review_status(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    status="APPROVED",
                )
        fetcher_dependency.assert_not_called()

    async def test_edit_pull_request_comment_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import edit_pull_request_comment

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await edit_pull_request_comment(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    comment_id=9,
                    text="x",
                    version=3,
                )
        fetcher_dependency.assert_not_called()

    async def test_resolve_pull_request_comment_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import resolve_pull_request_comment

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await resolve_pull_request_comment(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    comment_id=9,
                    version=3,
                )
        fetcher_dependency.assert_not_called()

    async def test_resolve_pull_request_task_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import resolve_pull_request_task

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await resolve_pull_request_task(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    comment_id=9,
                    version=3,
                )
        fetcher_dependency.assert_not_called()

    async def test_delete_pull_request_comment_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import delete_pull_request_comment

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await delete_pull_request_comment(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    comment_id=9,
                    version=3,
                )
        fetcher_dependency.assert_not_called()

    async def test_merge_pull_request_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import merge_pull_request

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await merge_pull_request(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    version=3,
                )
        fetcher_dependency.assert_not_called()

    async def test_decline_pull_request_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import decline_pull_request

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await decline_pull_request(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    version=3,
                )
        fetcher_dependency.assert_not_called()

    async def test_reopen_pull_request_blocked(self):
        from src.mcp_atlassian.servers.bitbucket import reopen_pull_request

        fetcher_dependency = AsyncMock()
        with patch(
            "src.mcp_atlassian.servers.bitbucket.get_bitbucket_fetcher",
            fetcher_dependency,
        ):
            with pytest.raises(ToolError, match="read-only mode"):
                await reopen_pull_request(
                    _read_only_context(),
                    project_key="PROJ",
                    repository_slug="my-repo",
                    pull_request_id=5,
                    version=3,
                )
        fetcher_dependency.assert_not_called()


class TestFetcherOffload:
    """Every tool runs its fetcher call in a worker thread, off the event loop.

    The fetcher is a synchronous ``requests`` client, so a direct call inside
    ``async def`` blocks every other client for the length of the upstream
    round trip. The static sweep pins the routing for all tools; the runtime
    check proves the call leaves the event-loop thread.
    """

    @staticmethod
    def _tool_functions() -> dict[str, ast.AsyncFunctionDef]:
        from src.mcp_atlassian.servers import bitbucket as bitbucket_server

        module = ast.parse(inspect.getsource(bitbucket_server))
        tools: dict[str, ast.AsyncFunctionDef] = {}
        for node in module.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                call = decorator.func if isinstance(decorator, ast.Call) else None
                if (
                    isinstance(call, ast.Attribute)
                    and call.attr == "tool"
                    and isinstance(call.value, ast.Name)
                    and call.value.id == "bitbucket_mcp"
                ):
                    tools[node.name] = node
        return tools

    @staticmethod
    def _fetcher_names(function: ast.AsyncFunctionDef) -> set[str]:
        """Return the names a tool binds to ``await get_bitbucket_fetcher(ctx)``."""
        names: set[str] = set()
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Await)
                and isinstance(node.value.value, ast.Call)
                and isinstance(node.value.value.func, ast.Name)
                and node.value.value.func.id == "get_bitbucket_fetcher"
            ):
                names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
        return names

    def test_every_tool_awaits_the_offload_helper(self):
        tools = self._tool_functions()
        # The registered tool count is part of the contract: a new tool must
        # be routed through the helper and this pin bumped in the same change.
        assert len(tools) == 33

        for name, function in tools.items():
            fetcher_names = self._fetcher_names(function)
            assert fetcher_names, f"{name} does not bind the fetcher"
            direct_calls = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in fetcher_names
            ]
            assert not direct_calls, f"{name} calls the fetcher on the event loop"

            offloaded = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "run_bitbucket_fetcher_call"
            ]
            assert offloaded, f"{name} does not await run_bitbucket_fetcher_call"

    @pytest.mark.anyio
    async def test_fetcher_call_runs_off_the_event_loop_thread(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        loop_thread = threading.get_ident()
        call_threads: list[int] = []
        page = mock_bitbucket_fetcher.list_projects.return_value

        def record_thread(**kwargs):
            call_threads.append(threading.get_ident())
            return page

        mock_bitbucket_fetcher.list_projects.side_effect = record_thread

        response = await bitbucket_client.call_tool("bitbucket_list_projects", {})

        assert _result_json(response)["count"] == 1
        assert call_threads and call_threads[0] != loop_thread


@pytest.mark.anyio
class TestGetCurrentUser:
    """The get_current_user tool."""

    async def test_success(self, bitbucket_client, mock_bitbucket_fetcher):
        """A successful call returns the caller's profile directly."""
        response = await bitbucket_client.call_tool("bitbucket_get_current_user", {})

        mock_bitbucket_fetcher.get_current_user_profile.assert_called_once_with(
            refresh=False
        )
        assert _result_json(response) == {
            "name": "jdoe",
            "slug": "jdoe-slug",
            "id": 101,
            "display_name": "J. Doe",
            "active": True,
            "type": "NORMAL",
        }

    async def test_refresh_is_forwarded(self, bitbucket_client, mock_bitbucket_fetcher):
        """The refresh flag reaches the fetcher."""
        await bitbucket_client.call_tool(
            "bitbucket_get_current_user", {"refresh": True}
        )

        mock_bitbucket_fetcher.get_current_user_profile.assert_called_once_with(
            refresh=True
        )

    async def test_is_read_only_and_in_the_users_toolset(self, bitbucket_client):
        """The tool is tagged read-only and belongs to the users toolset."""
        from src.mcp_atlassian.servers.bitbucket import bitbucket_mcp

        tool = await bitbucket_mcp.get_tool("get_current_user")
        assert "toolset:bitbucket_users" in tool.tags
        assert "read" in tool.tags
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True

    async def test_auth_error_preserved(self, bitbucket_client, mock_bitbucket_fetcher):
        """An authentication error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.get_current_user_profile.side_effect = (
            MCPAtlassianAuthenticationError("token rejected")
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool("bitbucket_get_current_user", {})

        assert "token rejected" in str(excinfo.value)

    async def test_not_found_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """A not-found error surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.get_current_user_profile.side_effect = (
            BitbucketResourceNotFoundError(
                "Bitbucket resource not found (HTTP 404) for the user."
            )
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool("bitbucket_get_current_user", {})

        assert "Bitbucket resource not found (HTTP 404)" in str(excinfo.value)

    async def test_value_error_preserved(
        self, bitbucket_client, mock_bitbucket_fetcher
    ):
        """An identity-resolution failure surfaces as a ToolError with its message."""
        mock_bitbucket_fetcher.get_current_user_profile.side_effect = ValueError(
            "Could not determine the authenticated user: the Bitbucket instance "
            "did not return an X-AUSERNAME header."
        )

        with pytest.raises(ToolError) as excinfo:
            await bitbucket_client.call_tool("bitbucket_get_current_user", {})

        assert "X-AUSERNAME" in str(excinfo.value)
