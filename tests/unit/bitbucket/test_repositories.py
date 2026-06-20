"""Unit tests for ReposMixin.list_repositories."""

from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.repositories import MAX_REPOS_LIMIT
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketRepository
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig


def _repo(slug, project_key="PROJ"):
    """Build a minimal RestRepository-shaped dict."""
    return {
        "slug": slug,
        "id": 1,
        "name": slug,
        "scmId": "git",
        "state": "AVAILABLE",
        "project": {"key": project_key, "id": 2, "name": project_key},
    }


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of the repos endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def _http_error_response(status: int) -> MagicMock:
    """Build a mock response whose raise_for_status raises an HTTPError."""
    response = MagicMock()
    response.status_code = status
    error = HTTPError(f"{status} error")
    error.response = response
    response.raise_for_status.side_effect = error
    return response


def _fetcher() -> BitbucketFetcher:
    """Build a DC BYO-token fetcher for repository tests."""
    return BitbucketFetcher(
        config=BitbucketConfig(
            url="https://bitbucket.corp.example.com",
            auth_type="oauth",
            oauth_config=BYOAccessTokenOAuthConfig(
                access_token="user-bearer-token",
                base_url="https://bitbucket.corp.example.com",
            ),
        )
    )


class TestListRepositories:
    """Happy-path listing, model mapping, and single-window pagination."""

    def test_returns_repository_models(self):
        """A page is mapped to BitbucketRepository models at the project path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_repo("api"), _repo("web")], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.list_repositories("PROJ", limit=10)

        assert all(isinstance(r, BitbucketRepository) for r in page.repositories)
        assert [r.slug for r in page.repositories] == ["api", "web"]
        assert page.repositories[0].project is not None
        assert page.repositories[0].project.key == "PROJ"
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects/PROJ/repos")
        # Single window: the page size sent upstream is the requested limit.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 10}

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_repo("a")], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_repositories("PROJ", limit=25)

        assert [r.slug for r in page.repositories] == ["a"]
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 25
        assert mock_get.call_count == 1

    def test_start_resumes_from_cursor(self):
        """A start offset is sent as the upstream start param."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_repo("b")], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_repositories("PROJ", start=25, limit=25)

        assert mock_get.call_args[1]["params"] == {"start": 25, "limit": 25}
        assert page.is_last_page is True
        assert page.next_page_start is None

    def test_truncates_at_limit(self):
        """A full window with more upstream stops at one request, flags truncation."""
        fetcher = _fetcher()
        big_page = _page_response(
            [_repo(f"r{i}") for i in range(25)],
            is_last_page=False,
            next_page_start=25,
        )
        with patch.object(fetcher._session, "get", return_value=big_page) as mock_get:
            page = fetcher.list_repositories("PROJ", limit=25)

        assert len(page.repositories) == 25
        assert page.truncated is True
        assert page.is_last_page is False
        assert page.next_page_start == 25
        assert mock_get.call_count == 1

    def test_empty_project_returns_empty_complete_result(self):
        """A valid project with no repos yields an empty, complete page."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ):
            page = fetcher.list_repositories("PROJ")

        assert page.repositories == []
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

    def test_limit_clamped_to_max(self):
        """A limit above the cap is clamped to MAX_REPOS_LIMIT for the window."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_repo("a")], is_last_page=True),
        ) as mock_get:
            fetcher.list_repositories("PROJ", limit=10_000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_REPOS_LIMIT


class TestListRepositoriesNameFilter:
    """The server-side ``name`` filter routes to the cross-project search."""

    def test_name_targets_cross_project_repos_endpoint(self):
        """With a name, the request hits /repos with projectkey + name params."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_repo("api")], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_repositories("PROJ", name="api", limit=10)

        called_url = mock_get.call_args[0][0]
        # Cross-project search endpoint, not the project-scoped path.
        assert called_url.endswith("/rest/api/1.0/repos")
        assert not called_url.endswith("/projects/PROJ/repos")
        params = mock_get.call_args[1]["params"]
        # Always scoped by projectkey (never an unscoped all-repos scan).
        assert params["projectkey"] == "PROJ"
        assert params["name"] == "api"
        assert params["start"] == 0
        assert params["limit"] == 10
        # Same model parses the cross-project response.
        assert [r.slug for r in page.repositories] == ["api"]
        assert isinstance(page.repositories[0], BitbucketRepository)

    def test_name_filtered_path_is_single_window(self):
        """The name path is a single upstream GET that surfaces the cursor."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_repo("api")], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_repositories("PROJ", name="api", start=25, limit=25)

        assert mock_get.call_count == 1
        assert mock_get.call_args[1]["params"]["start"] == 25
        assert page.next_page_start == 25
        assert page.is_last_page is False
        assert page.truncated is True

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_blank_name_uses_project_scoped_path(self, name):
        """A None/blank name keeps the project-scoped path with no extra params."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_repo("api")], is_last_page=True),
        ) as mock_get:
            fetcher.list_repositories("PROJ", name=name)

        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects/PROJ/repos")
        # No projectkey/name query params on the unfiltered path.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 25}

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_even_with_name(self, bad_key):
        """A blank project_key is rejected before any request, even with a name."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="non-empty"):
                fetcher.list_repositories(bad_key, name="api")
        mock_get.assert_not_called()


class TestListRepositoriesValidation:
    """project_key validation and path encoding."""

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="non-empty"):
                fetcher.list_repositories(bad_key)
        mock_get.assert_not_called()

    @pytest.mark.parametrize(
        "project_key, expected_segment",
        [
            ("a/b", "a%2Fb"),  # slash must not create extra path segments
            ("../admin", "..%2Fadmin"),  # path traversal is neutralised
            ("a b", "a%20b"),  # spaces encoded
        ],
    )
    def test_project_key_is_url_encoded(self, project_key, expected_segment):
        """The caller-supplied project_key is percent-encoded into the path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_repositories(project_key)

        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(f"/projects/{expected_segment}/repos")

    def test_surrounding_whitespace_is_stripped(self):
        """A padded key is stripped before encoding (not encoded as spaces)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_repositories("  PROJ  ")

        assert mock_get.call_args[0][0].endswith("/projects/PROJ/repos")


class TestListRepositoriesErrors:
    """The _get error taxonomy applies through the mixin."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_authentication_error(self, status):
        """A 401/403 surfaces as MCPAtlassianAuthenticationError."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(status)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match=str(status)):
                fetcher.list_repositories("PROJ")

    def test_server_error_raises_value_error(self):
        """A 5xx surfaces as a descriptive ValueError (not the auth error)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(503)
        ):
            with pytest.raises(ValueError, match="HTTP 503"):
                fetcher.list_repositories("PROJ")
