"""Unit tests for RefsMixin (branches, tags, default branch)."""

from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.refs import MAX_REFS_LIMIT
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketBranch, BitbucketTag
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import attach_body, attach_json

FULL_SHA = "8d51122def5632836d1cb1026e879069e10a1e13"


def _branch(display_id, latest_commit="abc123", is_default=False):
    """Build a minimal RestBranch-shaped dict."""
    return {
        "id": f"refs/heads/{display_id}",
        "displayId": display_id,
        "latestCommit": latest_commit,
        "latestChangeset": latest_commit,  # legacy alias the model must drop
        "type": "BRANCH",
        "isDefault": is_default,  # the live wire key (spec documents `default`)
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
    attach_json(response, body)
    return response


def _object_response(body):
    """Build a mock response carrying a single JSON object."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    attach_json(response, body)
    return response


def _http_error_response(status: int, body=None) -> MagicMock:
    """Build a mock response whose raise_for_status raises an HTTPError."""
    response = MagicMock()
    response.status_code = status
    error = HTTPError(f"{status} error")
    error.response = response
    response.raise_for_status.side_effect = error
    if body is not None:
        attach_json(response, body)
    return response


def _no_content_response() -> MagicMock:
    """Build a mock 204 response with an empty body."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    return attach_body(response, b"")


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
        assert params["orderBy"] == "MODIFICATION"  # matched case-insensitively
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

    def test_unknown_order_by_raises_without_request(self):
        """An unrecognised order_by raises before any request is made."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(
                ValueError, match="order_by must be one of ALPHABETICAL, MODIFICATION"
            ):
                fetcher.list_branches("PROJ", "my-repo", order_by="SIZE")
        mock_get.assert_not_called()

    @pytest.mark.parametrize("order_by", ["", "   "])
    def test_blank_order_by_is_dropped(self, order_by):
        """A blank order_by is omitted so the server default applies."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_branch("main")], is_last_page=True),
        ) as mock_get:
            fetcher.list_branches("PROJ", "my-repo", order_by=order_by)

        assert "orderBy" not in mock_get.call_args[1]["params"]

    def test_unknown_order_by_raises_for_tags_too(self):
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="order_by must be one of"):
                fetcher.list_tags("PROJ", "my-repo", order_by="SIZE")
        mock_get.assert_not_called()

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

    def test_slashy_tag_name_keeps_literal_separators(self):
        """A slash in a tag name stays a path separator; components are encoded."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_object_response(_tag("release/1.0 rc")),
        ) as mock_get:
            fetcher.get_tag("PROJ", "my-repo", "release/1.0 rc")

        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/tags/release/1.0%20rc")
        assert "%2F" not in called_url

    def test_padded_tag_name_is_stripped_before_encoding(self):
        """Surrounding whitespace is trimmed rather than encoded into the path."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_object_response(_tag("v1.0")),
        ) as mock_get:
            fetcher.get_tag("PROJ", "my-repo", " v1.0\n")

        assert mock_get.call_args[0][0].endswith("/tags/v1.0")

    @pytest.mark.parametrize("bad_name", ["../v1", "release//1.0", "./v1"])
    def test_traversal_tag_name_raises_without_request(self, bad_name):
        """An empty, '.', or '..' component is rejected before any request."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="tag name"):
                fetcher.get_tag("PROJ", "my-repo", bad_name)
        mock_get.assert_not_called()

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


class TestCreateBranch:
    """create_branch: request body, ref-name validation, and confirmation."""

    def test_posts_short_name_and_start_point(self):
        """The body carries the short name and start point; message is omitted."""
        fetcher = _fetcher()
        body = _branch("feature/x", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ) as mock_post:
            branch = fetcher.create_branch("PROJ", "my-repo", "feature/x", "main")

        assert mock_post.call_args[0][0].endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/branches"
        )
        assert mock_post.call_args[1]["json"] == {
            "name": "feature/x",
            "startPoint": "main",
        }
        assert isinstance(branch, BitbucketBranch)
        assert branch.display_id == "feature/x"
        assert branch.id == "refs/heads/feature/x"
        assert branch.latest_commit == FULL_SHA

    def test_message_is_sent_when_given(self):
        """A non-blank message is forwarded; a blank one is dropped."""
        fetcher = _fetcher()
        body = _branch("topic", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ) as mock_post:
            fetcher.create_branch("PROJ", "my-repo", "topic", "main", message="why")
            fetcher.create_branch("PROJ", "my-repo", "topic", "main", message="  ")

        first, second = (call[1]["json"] for call in mock_post.call_args_list)
        assert first["message"] == "why"
        assert "message" not in second

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "   ",
            "refs/heads/topic",
            "a..b",
            "-topic",
            "topic.lock",
            "dir/topic.lock/x",
            "has space",
            "bad~1",
            "bad^2",
            "bad:x",
            "bad?",
            "bad*",
            "bad[",
            "bad\\x",
            "bad@{1}",
            "/leading",
            "trailing/",
            "double//slash",
            "ctrl\x01",
            "del\x7f",
            "@",
            ".hidden",
            "dir/.hidden",
            "trailing.",
        ],
    )
    def test_invalid_name_rejected_before_request(self, name):
        """An invalid git ref name raises before any request is issued."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError):
                fetcher.create_branch("PROJ", "my-repo", name, "main")
        mock_post.assert_not_called()

    def test_non_ascii_name_is_sent_verbatim(self):
        """Non-ASCII letters are valid in git ref names and pass through."""
        fetcher = _fetcher()
        body = _branch("ветка/ünïcödé", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ) as mock_post:
            fetcher.create_branch("PROJ", "my-repo", "ветка/ünïcödé", "main")
        assert mock_post.call_args[1]["json"]["name"] == "ветка/ünïcödé"

    def test_surrounding_whitespace_is_stripped_before_send(self):
        """A padded name is stripped, sent, and confirmed against the stripped value."""
        fetcher = _fetcher()
        body = _branch("topic", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ) as mock_post:
            branch = fetcher.create_branch("PROJ", "my-repo", "  topic  ", "main")
        assert mock_post.call_args[1]["json"]["name"] == "topic"
        assert branch.display_id == "topic"

    def test_abbreviated_commit_start_point_is_sent_as_a_ref(self):
        """A short hex start point is forwarded as given and confirmed as hex only."""
        fetcher = _fetcher()
        body = _branch("topic", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ) as mock_post:
            fetcher.create_branch("PROJ", "my-repo", "topic", FULL_SHA[:7])
        assert mock_post.call_args[1]["json"]["startPoint"] == FULL_SHA[:7]

    def test_oversized_message_rejected_before_request(self):
        """A message over the cap raises before any request is issued."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="message must be at most"):
                fetcher.create_branch(
                    "PROJ", "my-repo", "topic", "main", message="x" * 32769
                )
        mock_post.assert_not_called()

    def test_confirmation_error_caps_upstream_value(self):
        """An oversized upstream displayId is truncated in the error message."""
        fetcher = _fetcher()
        body = _branch("z" * 500, latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ):
            with pytest.raises(ValueError) as excinfo:
                fetcher.create_branch("PROJ", "my-repo", "topic", "main")
        assert "..." in str(excinfo.value)
        assert len(str(excinfo.value)) < 300

    def test_blank_start_point_rejected_before_request(self):
        """A blank start point raises before any request is issued."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="start_point"):
                fetcher.create_branch("PROJ", "my-repo", "topic", "  ")
        mock_post.assert_not_called()

    def test_display_id_mismatch_raises(self):
        """A mismatched branch acknowledgement raises an error."""
        fetcher = _fetcher()
        body = _branch("other", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ):
            with pytest.raises(ValueError, match="not confirmed") as excinfo:
                fetcher.create_branch("PROJ", "my-repo", "topic", "main")
        assert "'displayId' 'topic'" in str(excinfo.value)

    @pytest.mark.parametrize("latest_commit", [None, "", "not-hex", 42])
    def test_non_hex_latest_commit_raises(self, latest_commit):
        """A 2xx body without a hexadecimal latestCommit is unconfirmed."""
        fetcher = _fetcher()
        body = _branch("topic")
        body["latestCommit"] = latest_commit
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ):
            with pytest.raises(ValueError, match="not confirmed"):
                fetcher.create_branch("PROJ", "my-repo", "topic", "main")

    def test_full_sha_start_point_must_match_latest_commit(self):
        """When the start point is a full commit id, latestCommit must equal it."""
        fetcher = _fetcher()
        body = _branch("topic", latest_commit="a" * 40)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ):
            with pytest.raises(ValueError, match="not confirmed"):
                fetcher.create_branch("PROJ", "my-repo", "topic", FULL_SHA)

    def test_full_sha_start_point_matches_case_insensitively(self):
        """A matching latestCommit in a different case is confirmed."""
        fetcher = _fetcher()
        body = _branch("topic", latest_commit=FULL_SHA.upper())
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ):
            branch = fetcher.create_branch("PROJ", "my-repo", "topic", FULL_SHA)
        assert branch.latest_commit == FULL_SHA.upper()

    def test_non_object_body_raises(self):
        """A 2xx body that is not an object is reported as unexpected."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "post", return_value=_object_response([1])):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.create_branch("PROJ", "my-repo", "topic", "main")

    def test_conflict_surfaces_instance_message(self):
        """A 409 (name already exists) surfaces the instance's own message."""
        fetcher = _fetcher()
        body = {"errors": [{"message": "A branch named 'topic' already exists."}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_response(409, body)
        ):
            with pytest.raises(ValueError, match="already exists"):
                fetcher.create_branch("PROJ", "my-repo", "topic", "main")


class TestDeleteBranch:
    """delete_branch: the branch-utils body-addressed delete."""

    def test_sends_json_body_under_branch_utils(self):
        """A short name is qualified and sent with endPoint and dryRun."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "delete", return_value=_no_content_response()
        ) as mock_delete:
            result = fetcher.delete_branch("PROJ", "my-repo", "feature/x", FULL_SHA)

        assert mock_delete.call_args[0][0].endswith(
            "/rest/branch-utils/1.0/projects/PROJ/repos/my-repo/branches"
        )
        assert mock_delete.call_args[1]["json"] == {
            "name": "refs/heads/feature/x",
            "endPoint": FULL_SHA,
            "dryRun": False,
        }
        assert result["branch"] == "refs/heads/feature/x"
        assert result["end_point"] == FULL_SHA
        assert result["dry_run"] is False
        assert result["accepted"] is True
        assert "list_branches" in result["note"]

    def test_qualified_name_sent_as_given(self):
        """A refs/heads/... id is not double-prefixed."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "delete", return_value=_no_content_response()
        ) as mock_delete:
            fetcher.delete_branch("PROJ", "my-repo", "refs/heads/topic", FULL_SHA)

        assert mock_delete.call_args[1]["json"]["name"] == "refs/heads/topic"

    def test_dry_run_forwarded_and_reported(self):
        """dry_run=True is sent as a JSON boolean and reported as not deleted."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "delete", return_value=_no_content_response()
        ) as mock_delete:
            result = fetcher.delete_branch(
                "PROJ", "my-repo", "topic", FULL_SHA, dry_run=True
            )

        assert mock_delete.call_args[1]["json"]["dryRun"] is True
        assert result["dry_run"] is True
        assert result["accepted"] is True
        assert result["note"] == "Dry run: nothing was deleted."

    @pytest.mark.parametrize(
        "end_point", ["", "main", "abc123", FULL_SHA[:39], FULL_SHA + "0", "g" * 40]
    )
    def test_end_point_must_be_full_commit_id(self, end_point):
        """Anything but a 40-character hexadecimal id raises before any request."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "delete") as mock_delete:
            with pytest.raises(ValueError, match="end_point"):
                fetcher.delete_branch("PROJ", "my-repo", "topic", end_point)
        mock_delete.assert_not_called()

    @pytest.mark.parametrize(
        "name", ["", "refs/tags/v1", "refs/heads/", "a..b", "-x", "x.lock", "a b"]
    )
    def test_invalid_name_rejected_before_request(self, name):
        """A non-branch or invalid ref name raises before any request."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "delete") as mock_delete:
            with pytest.raises(ValueError):
                fetcher.delete_branch("PROJ", "my-repo", name, FULL_SHA)
        mock_delete.assert_not_called()

    def test_end_point_mismatch_surfaces_instance_message(self):
        """The server's 400 for a branch pointing elsewhere is reported."""
        fetcher = _fetcher()
        body = {"errors": [{"message": "Branch 'topic' points to a different commit"}]}
        with patch.object(
            fetcher._session, "delete", return_value=_http_error_response(400, body)
        ):
            with pytest.raises(ValueError, match="different commit"):
                fetcher.delete_branch("PROJ", "my-repo", "topic", FULL_SHA)

    def test_body_on_2xx_is_unconfirmed(self):
        """A 2xx carrying a body matches no documented success and raises."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "delete", return_value=_object_response({"x": 1})
        ):
            with pytest.raises(ValueError, match="not confirmed"):
                fetcher.delete_branch("PROJ", "my-repo", "topic", FULL_SHA)

    def test_auth_status_raises_authentication_error(self):
        """A 401 (missing REPO_WRITE or branch permission) is the auth error."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "delete", return_value=_http_error_response(401)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match="401"):
                fetcher.delete_branch("PROJ", "my-repo", "topic", FULL_SHA)


class TestCreateTag:
    """create_tag: request body, ref-name validation, and confirmation."""

    def test_posts_short_name_start_point_and_message(self):
        """The body carries the short name, start point, and message."""
        fetcher = _fetcher()
        body = _tag("v1.2.0", latest_commit=FULL_SHA, hash_="objsha")
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ) as mock_post:
            tag = fetcher.create_tag(
                "PROJ", "my-repo", "v1.2.0", FULL_SHA, message="Release 1.2.0"
            )

        assert mock_post.call_args[0][0].endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/tags"
        )
        assert mock_post.call_args[1]["json"] == {
            "name": "v1.2.0",
            "startPoint": FULL_SHA,
            "message": "Release 1.2.0",
        }
        assert isinstance(tag, BitbucketTag)
        assert tag.display_id == "v1.2.0"
        assert tag.hash == "objsha"

    def test_lightweight_tag_omits_message(self):
        """Without a message the body carries only name and startPoint."""
        fetcher = _fetcher()
        body = _tag("v1", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ) as mock_post:
            fetcher.create_tag("PROJ", "my-repo", "v1", "main")

        assert mock_post.call_args[1]["json"] == {"name": "v1", "startPoint": "main"}

    @pytest.mark.parametrize("name", ["", "refs/tags/v1", "v..1", "-v1", "v1.lock"])
    def test_invalid_name_rejected_before_request(self, name):
        """An invalid git ref name raises before any request is issued."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError):
                fetcher.create_tag("PROJ", "my-repo", name, "main")
        mock_post.assert_not_called()

    def test_display_id_mismatch_raises(self):
        """A mismatched tag acknowledgement raises an error."""
        fetcher = _fetcher()
        body = _tag("v9", latest_commit=FULL_SHA)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ):
            with pytest.raises(ValueError, match="not confirmed"):
                fetcher.create_tag("PROJ", "my-repo", "v1", "main")

    def test_full_sha_start_point_must_match_latest_commit(self):
        """When the start point is a full commit id, latestCommit must equal it."""
        fetcher = _fetcher()
        body = _tag("v1", latest_commit="b" * 40)
        with patch.object(
            fetcher._session, "post", return_value=_object_response(body)
        ):
            with pytest.raises(ValueError, match="not confirmed"):
                fetcher.create_tag("PROJ", "my-repo", "v1", FULL_SHA)
