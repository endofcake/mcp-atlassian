"""Unit tests for CommitsMixin (list_commits, get_commit, PR commits)."""

from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.commits import MAX_COMMITS_LIMIT
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketCommit
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig


def _commit(display_id, *, message="msg", parents=("p1",)):
    """Build a minimal RestCommit-shaped dict."""
    return {
        "id": f"{display_id}fullsha",
        "displayId": display_id,
        "message": message,
        "author": {"name": "Dev", "emailAddress": "dev@x"},
        "authorTimestamp": 1700000000000,
        "committer": {"name": "Dev", "emailAddress": "dev@x"},
        "committerTimestamp": 1700000100000,
        "parents": [{"id": p, "displayId": p} for p in parents],
    }


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of a commits endpoint."""
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
    """Build a DC BYO-token fetcher for commit tests."""
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


class TestListCommits:
    """Happy-path listing, model mapping, and single-window pagination."""

    def test_returns_commit_models(self):
        """A page is mapped to BitbucketCommit models at the commits path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_commit("aaa"), _commit("bbb")], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.list_commits("PROJ", "my-repo", limit=10)

        assert all(isinstance(c, BitbucketCommit) for c in page.commits)
        assert [c.display_id for c in page.commits] == ["aaa", "bbb"]
        assert page.commits[0].author is not None
        assert page.commits[0].author.name == "Dev"
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects/PROJ/repos/my-repo/commits")
        # Single window: the page size sent upstream is the requested limit.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 10}

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_commit("aaa")], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_commits("PROJ", "my-repo", limit=25)

        assert [c.display_id for c in page.commits] == ["aaa"]
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
            return_value=_page_response([_commit("ccc")], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_commits("PROJ", "my-repo", start=25, limit=25)

        assert mock_get.call_args[1]["params"] == {"start": 25, "limit": 25}
        assert page.is_last_page is True
        assert page.next_page_start is None

    def test_history_filters_land_in_params(self):
        """since/until/path land verbatim (stripped) in the wire params."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            fetcher.list_commits(
                "PROJ",
                "my-repo",
                since="oldsha",
                until="  main  ",
                path=" src/app.py ",
                limit=10,
            )

        params = mock_get.call_args[1]["params"]
        assert params["since"] == "oldsha"
        assert params["until"] == "main"  # stripped
        assert params["path"] == "src/app.py"  # stripped
        assert params["start"] == 0
        assert params["limit"] == 10

    def test_bool_and_merges_params_serialized_lowercase(self):
        """merges normalises; follow_renames/ignore_missing are lowercase strings."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            fetcher.list_commits(
                "PROJ",
                "my-repo",
                merges="  ONLY  ",
                follow_renames=True,
                ignore_missing=False,
                path="src/app.py",
            )

        params = mock_get.call_args[1]["params"]
        assert params["merges"] == "only"  # normalised + lowercased
        assert params["followRenames"] == "true"
        assert params["ignoreMissing"] == "false"

    def test_unknown_merges_is_dropped(self):
        """An unrecognised merges value is dropped (server default applies)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            fetcher.list_commits("PROJ", "my-repo", merges="sometimes")

        assert "merges" not in mock_get.call_args[1]["params"]

    def test_unset_filters_absent_from_params(self):
        """Unset filter params are not sent at all."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            fetcher.list_commits("PROJ", "my-repo")

        params = mock_get.call_args[1]["params"]
        assert params == {"start": 0, "limit": 25}
        for key in (
            "since",
            "until",
            "path",
            "merges",
            "followRenames",
            "ignoreMissing",
        ):
            assert key not in params

    def test_limit_clamped_to_max(self):
        """A limit above the cap is clamped to MAX_COMMITS_LIMIT for the window."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            fetcher.list_commits("PROJ", "my-repo", limit=10_000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_COMMITS_LIMIT

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.list_commits(bad_key, "my-repo")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_slug", ["", "   "])
    def test_blank_slug_raises_without_request(self, bad_slug):
        """A blank repository_slug raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="repository_slug"):
                fetcher.list_commits("PROJ", bad_slug)
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_path", [None, "", "   "])
    def test_follow_renames_without_path_raises_without_request(self, bad_path):
        """follow_renames without a single-file path raises before any request."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="follow_renames requires"):
                fetcher.list_commits(
                    "PROJ", "my-repo", follow_renames=True, path=bad_path
                )
        mock_get.assert_not_called()


class TestGetCommit:
    """Single-commit lookup, id encoding, and the no-path invariant."""

    def test_returns_commit_model_and_omits_path(self):
        """A commit object is parsed; no `path` param is ever sent."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_object_response(_commit("abc123d")),
        ) as mock_get:
            commit = fetcher.get_commit("PROJ", "my-repo", "abc123dfullsha")

        assert isinstance(commit, BitbucketCommit)
        assert commit.display_id == "abc123d"
        assert mock_get.call_count == 1
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/commits/abc123dfullsha"
        )
        # get-by-id is strict: the path gotcha is avoided by omitting `path`.
        params = mock_get.call_args[1].get("params")
        assert params is None or "path" not in params

    def test_commit_id_is_encoded(self):
        """A commit id with special characters is percent-encoded into the path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_object_response(_commit("ref")),
        ) as mock_get:
            fetcher.get_commit("PROJ", "my-repo", "refs/heads/main")

        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/commits/refs%2Fheads%2Fmain")

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.get_commit(bad_key, "my-repo", "sha")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_slug", ["", "   "])
    def test_blank_slug_raises_without_request(self, bad_slug):
        """A blank repository_slug raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="repository_slug"):
                fetcher.get_commit("PROJ", bad_slug, "sha")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_id", ["", "   "])
    def test_blank_commit_id_raises_without_request(self, bad_id):
        """A blank commit id raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="commit_id"):
                fetcher.get_commit("PROJ", "my-repo", bad_id)
        mock_get.assert_not_called()

    def test_non_dict_body_raises_value_error(self):
        """A non-object body surfaces as a descriptive ValueError."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_object_response(["not", "a", "dict"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_commit("PROJ", "my-repo", "sha")


class TestGetPullRequestCommits:
    """PR-commit listing, id coercion, and single-window pagination."""

    def test_returns_commit_models(self):
        """A page is mapped at the PR-commits path with the coerced id."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            page = fetcher.get_pull_request_commits("PROJ", "my-repo", 42, limit=10)

        assert [c.display_id for c in page.commits] == ["aaa"]
        assert page.is_last_page is True
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/42/commits"
        )
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 10}

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_commit("aaa")], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.get_pull_request_commits(
                "PROJ", "my-repo", 42, start=10, limit=25
            )

        assert page.next_page_start == 25
        assert page.is_last_page is False
        assert page.truncated is True
        assert mock_get.call_count == 1
        assert mock_get.call_args[1]["params"]["start"] == 10

    def test_string_pr_id_is_coerced(self):
        """A numeric-string id is coerced into the path as an integer."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            fetcher.get_pull_request_commits("PROJ", "my-repo", "42")

        assert mock_get.call_args[0][0].endswith("/pull-requests/42/commits")

    def test_limit_clamped_to_max(self):
        """A limit above the cap is clamped to MAX_COMMITS_LIMIT."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_commit("aaa")], is_last_page=True),
        ) as mock_get:
            fetcher.get_pull_request_commits("PROJ", "my-repo", 42, limit=10_000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_COMMITS_LIMIT

    @pytest.mark.parametrize("bad_id", [0, -1, "abc", "  "])
    def test_invalid_pr_id_raises_without_request(self, bad_id):
        """A non-positive-int pr id raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="pull_request_id"):
                fetcher.get_pull_request_commits("PROJ", "my-repo", bad_id)
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_key", ["", "   "])
    def test_blank_project_key_raises_without_request(self, bad_key):
        """A blank project_key raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.get_pull_request_commits(bad_key, "my-repo", 42)
        mock_get.assert_not_called()

    @pytest.mark.parametrize("bad_slug", ["", "   "])
    def test_blank_slug_raises_without_request(self, bad_slug):
        """A blank repository_slug raises before any HTTP request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="repository_slug"):
                fetcher.get_pull_request_commits("PROJ", bad_slug, 42)
        mock_get.assert_not_called()


class TestCommitsErrors:
    """The _get error taxonomy applies through the mixin."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_authentication_error(self, status):
        """A 401/403 surfaces as MCPAtlassianAuthenticationError."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(status)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match=str(status)):
                fetcher.list_commits("PROJ", "my-repo")

    def test_not_found_raises_resource_not_found(self):
        """A 404 surfaces as BitbucketResourceNotFoundError (a ValueError)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(ValueError, match="not found"):
                fetcher.get_commit("PROJ", "my-repo", "missing-sha")

    def test_server_error_raises_value_error(self):
        """A 5xx surfaces as a descriptive ValueError (not the auth error)."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(503)
        ):
            with pytest.raises(ValueError, match="HTTP 503"):
                fetcher.get_pull_request_commits("PROJ", "my-repo", 42)
