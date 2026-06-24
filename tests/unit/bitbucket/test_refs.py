"""Unit tests for RefsMixin (branches, tags, default branch)."""

from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.refs import MAX_REFS_LIMIT
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketBranch, BitbucketTag
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig


def _branch(display_id, latest_commit="abc123", is_default=False):
    """Build a minimal RestBranch-shaped dict."""
    return {
        "id": f"refs/heads/{display_id}",
        "displayId": display_id,
        "latestCommit": latest_commit,
        "latestChangeset": latest_commit,  # legacy alias the model must drop
        "type": "BRANCH",
        "default": is_default,
    }


def _tag(display_id, latest_commit="abc123", hash_=None):
    """Build a minimal RestTag-shaped dict."""
    body = {
        "id": f"refs/tags/{display_id}",
        "displayId": display_id,
        "latestCommit": latest_commit,
        "latestChangeset": latest_commit,  # legacy alias the model must drop
        "type": "TAG",
    }
    if hash_ is not None:
        body["hash"] = hash_
    return body


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of a refs endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def _object_response(body):
    """Build a mock response carrying a single JSON object."""
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
    """Build a DC BYO-token fetcher for ref tests."""
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


class TestListBranches:
    """Happy-path listing, model mapping, and single-window pagination."""

    def test_returns_branch_models(self):
        """A page is mapped to BitbucketBranch models at the branches path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_branch("main", is_default=True), _branch("dev")],
                is_last_page=True,
            ),
        ) as mock_get:
            page = fetcher.list_branches("PROJ", "my-repo", limit=10)

        assert all(isinstance(b, BitbucketBranch) for b in page.branches)
        assert [b.display_id for b in page.branches] == ["main", "dev"]
        assert page.branches[0].is_default is True
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects/PROJ/repos/my-repo/branches")
        # Single window: the page size sent upstream is the requested limit.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 10}

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_branch("a")], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_branches("PROJ", "my-repo", limit=25)

        assert [b.display_id for b in page.branches] == ["a"]
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
            return_value=_page_response([_branch("b")], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_branches("PROJ", "my-repo", start=25, limit=25)

        assert mock_get.call_args[1]["params"] == {"start": 25, "limit": 25}
        assert page.is_last_page is True
        assert page.next_page_start is None

    def test_filter_order_and_boost_land_in_params(self):
        """filter_text/order_by/boost_matches are sent as the wire params."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_branch("main")], is_last_page=True),
        ) as mock_get:
            fetcher.list_branches(
                "PROJ",
                "my-repo",
                filter_text="  main  ",
                order_by="modification",
                boost_matches=True,
                limit=10,
            )

        params = mock_get.call_args[1]["params"]
        assert params["filterText"] == "main"  # stripped
        assert params["orderBy"] == "MODIFICATION"  # normalized + uppercased
        assert params["boostMatches"] == "true"
        assert params["start"] == 0
        assert params["limit"] == 10

    def test_unset_filters_absent_from_params(self):
        """Unset filter/order/boost params are not sent at all."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_branch("main")], is_last_page=True),
        ) as mock_get:
            fetcher.list_branches("PROJ", "my-repo")

        params = mock_get.call_args[1]["params"]
        assert params == {"start": 0, "limit": 25}
        assert "filterText" not in params
        assert "orderBy" not in params
        assert "boostMatches" not in params

    def test_unknown_order_by_is_dropped(self):
        """An unrecognised order_by is dropped (server default applies)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_branch("main")], is_last_page=True),
        ) as mock_get:
            fetcher.list_branches("PROJ", "my-repo", order_by="SIZE")

        assert "orderBy" not in mock_get.call_args[1]["params"]

    def test_boost_matches_false_sent_as_string(self):
        """A False boost_matches is sent as the lowercase string 'false'."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_branch("main")], is_last_page=True),
        ) as mock_get:
            fetcher.list_branches("PROJ", "my-repo", boost_matches=False)

        assert mock_get.call_args[1]["params"]["boostMatches"] == "false"

    def test_limit_clamped_to_max(self):
        """A limit above the cap is clamped to MAX_REFS_LIMIT for the window."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_branch("a")], is_last_page=True),
        ) as mock_get:
            fetcher.list_branches("PROJ", "my-repo", limit=10_000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_REFS_LIMIT

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.list_branches(bad_key, "my-repo")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_slug", ["", "   "])
    def test_blank_slug_raises_without_request(self, bad_slug):
        """A blank repository_slug raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="repository_slug"):
                fetcher.list_branches("PROJ", bad_slug)
        mock_get.assert_not_called()


class TestListTags:
    """Happy-path listing, model mapping, and single-window pagination."""

    def test_returns_tag_models(self):
        """A page is mapped to BitbucketTag models at the tags path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_tag("v1.0", hash_="objsha"), _tag("v2.0")],
                is_last_page=True,
            ),
        ) as mock_get:
            page = fetcher.list_tags("PROJ", "my-repo", limit=10)

        assert all(isinstance(t, BitbucketTag) for t in page.tags)
        assert [t.display_id for t in page.tags] == ["v1.0", "v2.0"]
        assert page.tags[0].hash == "objsha"
        assert page.tags[1].hash is None  # lightweight tag
        assert page.is_last_page is True
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects/PROJ/repos/my-repo/tags")
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 10}

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_tag("v1")], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_tags("PROJ", "my-repo", start=10, limit=25)

        assert [t.display_id for t in page.tags] == ["v1"]
        assert page.next_page_start == 25
        assert page.is_last_page is False
        assert page.truncated is True
        assert mock_get.call_count == 1
        assert mock_get.call_args[1]["params"]["start"] == 10

    def test_filter_and_order_land_in_params(self):
        """filter_text/order_by land in the outgoing params for tags too."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_tag("v1")], is_last_page=True),
        ) as mock_get:
            fetcher.list_tags(
                "PROJ", "my-repo", filter_text="v1", order_by="ALPHABETICAL"
            )

        params = mock_get.call_args[1]["params"]
        assert params["filterText"] == "v1"
        assert params["orderBy"] == "ALPHABETICAL"

    def test_limit_clamped_to_max(self):
        """A limit above the cap is clamped to MAX_REFS_LIMIT."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_tag("v1")], is_last_page=True),
        ) as mock_get:
            fetcher.list_tags("PROJ", "my-repo", limit=10_000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_REFS_LIMIT

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.list_tags(bad_key, "my-repo")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_slug", ["", "   "])
    def test_blank_slug_raises_without_request(self, bad_slug):
        """A blank repository_slug raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="repository_slug"):
                fetcher.list_tags("PROJ", bad_slug)
        mock_get.assert_not_called()


class TestGetTag:
    """Single-tag lookup, name encoding, and validation."""

    def test_returns_tag_model(self):
        """A tag object is parsed into a BitbucketTag."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_object_response(_tag("v1.0", hash_="objsha")),
        ) as mock_get:
            tag = fetcher.get_tag("PROJ", "my-repo", "v1.0")

        assert isinstance(tag, BitbucketTag)
        assert tag.display_id == "v1.0"
        assert tag.hash == "objsha"
        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0].endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/tags/v1.0"
        )

    def test_slashy_tag_name_is_encoded(self):
        """A tag name with slashes is percent-encoded into the path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_object_response(_tag("release/1.0")),
        ) as mock_get:
            fetcher.get_tag("PROJ", "my-repo", "release/1.0")

        called_url = mock_get.call_args[0][0]
        # The slash must not create an extra path segment.
        assert called_url.endswith("/tags/release%2F1.0")

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.get_tag(bad_key, "my-repo", "v1")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_name", ["", "   "])
    def test_blank_name_raises_without_request(self, bad_name):
        """A blank tag name raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="name"):
                fetcher.get_tag("PROJ", "my-repo", bad_name)
        mock_get.assert_not_called()

    def test_non_dict_body_raises_value_error(self):
        """A non-object body surfaces as a descriptive ValueError."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_object_response(["not", "a", "dict"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_tag("PROJ", "my-repo", "v1")


class TestGetDefaultBranch:
    """Default-branch resolution from a RestMinimalRef."""

    def test_parses_minimal_ref(self):
        """A RestMinimalRef (id/displayId/type, no commit) parses to a branch."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_object_response(
                {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH"}
            ),
        ) as mock_get:
            branch = fetcher.get_default_branch("PROJ", "my-repo")

        assert isinstance(branch, BitbucketBranch)
        assert branch.display_id == "main"
        assert branch.id == "refs/heads/main"
        assert branch.type == "BRANCH"
        assert branch.latest_commit is None  # minimal ref carries no commit
        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0].endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/default-branch"
        )

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.get_default_branch(bad_key, "my-repo")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_slug", ["", "   "])
    def test_blank_slug_raises_without_request(self, bad_slug):
        """A blank repository_slug raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="repository_slug"):
                fetcher.get_default_branch("PROJ", bad_slug)
        mock_get.assert_not_called()

    def test_non_dict_body_raises_value_error(self):
        """A non-object body surfaces as a descriptive ValueError."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_object_response("nonsense")
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_default_branch("PROJ", "my-repo")


class TestRefsErrors:
    """The _get error taxonomy applies through the mixin."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_authentication_error(self, status):
        """A 401/403 surfaces as MCPAtlassianAuthenticationError."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(status)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match=str(status)):
                fetcher.list_branches("PROJ", "my-repo")

    def test_not_found_raises_resource_not_found(self):
        """A 404 surfaces as BitbucketResourceNotFoundError (a ValueError)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(ValueError, match="not found"):
                fetcher.get_tag("PROJ", "my-repo", "v-missing")

    def test_server_error_raises_value_error(self):
        """A 5xx surfaces as a descriptive ValueError (not the auth error)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(503)
        ):
            with pytest.raises(ValueError, match="HTTP 503"):
                fetcher.list_tags("PROJ", "my-repo")
