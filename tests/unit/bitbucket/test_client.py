"""Unit tests for BitbucketFetcher."""

import json
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError
from requests.exceptions import ChunkedEncodingError, ReadTimeout, SSLError
from requests.exceptions import ConnectionError as RequestsConnectionError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.client import (
    _MAX_PROJECT_PAGES,
    MAX_PROJECTS_LIMIT,
    BitbucketResourceNotFoundError,
)
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of the projects endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def _byo_config(
    token: str = "user-bearer-token", projects_filter: str | None = None
) -> BitbucketConfig:
    """Build a DC BYO-token config for client construction."""
    return BitbucketConfig(
        url="https://bitbucket.corp.example.com",
        auth_type="oauth",
        oauth_config=BYOAccessTokenOAuthConfig(
            access_token=token,
            base_url="https://bitbucket.corp.example.com",
        ),
        projects_filter=projects_filter,
    )


class TestBitbucketFetcherInit:
    """Tests for BitbucketFetcher construction."""

    def test_session_carries_bearer_token(self):
        """The session is configured with the forwarded OAuth bearer token."""
        fetcher = BitbucketFetcher(config=_byo_config("user-bearer-token"))

        assert fetcher._session.headers["Authorization"] == "Bearer user-bearer-token"
        # Bearer credentials must not be overridden by .netrc (#860).
        assert fetcher._session.trust_env is False

    def test_api_root_uses_rest_1_0_base_path(self):
        """The API root joins the base URL with the DC core REST path."""
        fetcher = BitbucketFetcher(config=_byo_config())

        assert fetcher._api_root == "https://bitbucket.corp.example.com/rest/api/1.0"

    def test_missing_oauth_config_raises(self):
        """An OAuth config with no oauth_config raises a clear error."""
        config = BitbucketConfig(
            url="https://bitbucket.corp.example.com",
            auth_type="oauth",
            oauth_config=None,
        )
        with pytest.raises(ValueError, match="requires oauth_config"):
            BitbucketFetcher(config=config)


class TestBitbucketFetcherCalls:
    """Tests for the validation and list calls."""

    def test_get_current_user_hits_validation_endpoint(self):
        """get_current_user issues a GET against the inbox count endpoint."""
        fetcher = BitbucketFetcher(config=_byo_config())

        mock_response = MagicMock()
        mock_response.json.return_value = {"count": 0}
        with patch.object(
            fetcher._session, "get", return_value=mock_response
        ) as mock_get:
            result = fetcher.get_current_user()

        assert result == {"count": 0}
        called_url = mock_get.call_args[0][0]
        assert called_url == (
            "https://bitbucket.corp.example.com/rest/api/1.0/inbox/pull-requests/count"
        )

    def test_list_projects_returns_values(self):
        """list_projects unwraps the paged 'values' field on a single window."""
        fetcher = BitbucketFetcher(config=_byo_config())

        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [{"key": "PROJ"}, {"key": "TEAM"}], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.list_projects(limit=10)

        # list_projects returns BitbucketProject models (see migration note).
        assert [p.key for p in page.projects] == ["PROJ", "TEAM"]
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects")
        # Unfiltered: single window — page size sent upstream is the limit.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 10}


class TestListProjectsPagination:
    """list_projects single-window pagination and response-shape validation.

    Unfiltered, list_projects fetches one window per call and surfaces the
    upstream cursor; the multi-page capped walk is exercised under
    TestListProjectsFilter (where a client-side filter is configured).
    """

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [{"key": "A"}, {"key": "B"}], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_projects(limit=25)

        assert [p.key for p in page.projects] == ["A", "B"]
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 25
        assert mock_get.call_count == 1

    def test_start_resumes_from_cursor(self):
        """A start offset is sent as the upstream start param."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([{"key": "C"}], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_projects(start=25, limit=25)

        assert mock_get.call_args[1]["params"] == {"start": 25, "limit": 25}
        assert page.is_last_page is True
        assert page.next_page_start is None

    def test_truncates_at_limit_without_overfetching(self):
        """A full window with more upstream stops after one request, flags truncation."""
        fetcher = BitbucketFetcher(config=_byo_config())
        big_page = _page_response(
            [{"key": f"P{i}"} for i in range(25)],
            is_last_page=False,
            next_page_start=25,
        )
        with patch.object(fetcher._session, "get", return_value=big_page) as mock_get:
            page = fetcher.list_projects(limit=25)

        assert len(page.projects) == 25
        assert page.truncated is True
        assert page.is_last_page is False
        assert page.next_page_start == 25
        assert mock_get.call_count == 1

    def test_empty_last_page_is_not_truncated(self):
        """An empty, last page yields an empty, complete result (not an error)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ):
            page = fetcher.list_projects()

        assert page.projects == []
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

    def test_limit_clamped_to_max(self):
        """A limit above the cap is clamped to MAX_PROJECTS_LIMIT for the window."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([{"key": "A"}], is_last_page=True),
        ) as mock_get:
            fetcher.list_projects(limit=10_000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_PROJECTS_LIMIT

    def test_non_dict_response_raises_value_error(self):
        """A non-paged body (a bare list) is an error, not a silent empty result."""
        fetcher = BitbucketFetcher(config=_byo_config())
        bad = MagicMock()
        bad.raise_for_status.return_value = None
        bad.json.return_value = ["not", "a", "page"]
        with patch.object(fetcher._session, "get", return_value=bad):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.list_projects()

    def test_missing_values_field_raises_value_error(self):
        """A dict without a 'values' list is an error, not a silent empty result."""
        fetcher = BitbucketFetcher(config=_byo_config())
        bad = MagicMock()
        bad.raise_for_status.return_value = None
        bad.json.return_value = {"size": 0, "isLastPage": True}
        with patch.object(fetcher._session, "get", return_value=bad):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.list_projects()

    @pytest.mark.parametrize("projects_filter", [None, "PROJ"])
    def test_non_dict_value_entry_raises_value_error(self, projects_filter):
        """A non-dict entry in 'values' errors identically with or without a filter.

        A non-dict entry would otherwise crash the filter comprehension with an
        AttributeError (which the tool reports as a generic 'unexpected error'),
        while the unfiltered path passed it through. Both now raise the same
        shape error.
        """
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter=projects_filter))
        body = _page_response(["junk", {"key": "PROJ"}], is_last_page=True)
        with patch.object(fetcher._session, "get", return_value=body):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.list_projects()


class TestListProjectsFilter:
    """config.projects_filter narrows list_projects to an allowlist of keys."""

    def test_filter_restricts_to_allowlisted_keys(self):
        """Only projects whose key is in the filter are returned."""
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="PROJ,TEAM"))
        body = _page_response(
            [{"key": "PROJ"}, {"key": "OTHER"}, {"key": "TEAM"}], is_last_page=True
        )
        with patch.object(fetcher._session, "get", return_value=body):
            page = fetcher.list_projects()

        assert [p.key for p in page.projects] == ["PROJ", "TEAM"]
        assert page.is_last_page is True
        assert page.truncated is False

    def test_filter_is_case_insensitive(self):
        """A lower-case filter entry matches an upper-case Bitbucket project key."""
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="proj"))
        body = _page_response([{"key": "PROJ"}, {"key": "OTHER"}], is_last_page=True)
        with patch.object(fetcher._session, "get", return_value=body):
            page = fetcher.list_projects()

        assert [p.key for p in page.projects] == ["PROJ"]

    def test_filter_matches_are_collected_across_pages(self):
        """Pagination continues past non-matching pages to find filtered keys.

        The configured filter keeps the multi-page capped walk (not the
        single-window mode), so a match on a later page is still found and the
        completed walk surfaces no resume cursor.
        """
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="C"))
        pages = [
            _page_response(
                [{"key": "A"}, {"key": "B"}], is_last_page=False, next_page_start=2
            ),
            _page_response([{"key": "C"}], is_last_page=True),
        ]
        with patch.object(fetcher._session, "get", side_effect=pages) as mock_get:
            page = fetcher.list_projects()

        assert [p.key for p in page.projects] == ["C"]
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        # The capped walk really followed the cursor across pages.
        assert mock_get.call_count == 2

    def test_filtered_walk_stays_bounded_by_page_cap(self):
        """A never-matching filter stops at the page cap (the DOS guard).

        The filtered path must keep its bounded multi-page walk: an endless
        upstream with no match scans exactly _MAX_PROJECT_PAGES pages, then
        reports an incomplete (truncated, not last page) result with a resume
        cursor — never an unbounded scan.
        """
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="ZZZ"))

        def endless(url, params=None, timeout=None):
            start = params["start"]
            return _page_response(
                [{"key": f"P{start + i}"} for i in range(100)],
                is_last_page=False,
                next_page_start=start + 100,
            )

        with patch.object(fetcher._session, "get", side_effect=endless) as mock_get:
            page = fetcher.list_projects()

        assert mock_get.call_count == _MAX_PROJECT_PAGES
        assert page.projects == []
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == _MAX_PROJECT_PAGES * 100

    def test_blank_filter_returns_all_projects(self):
        """An empty/whitespace filter is treated as no filter."""
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="  "))
        body = _page_response([{"key": "PROJ"}, {"key": "OTHER"}], is_last_page=True)
        with patch.object(fetcher._session, "get", return_value=body):
            page = fetcher.list_projects()

        assert [p.key for p in page.projects] == ["PROJ", "OTHER"]


class TestListProjectsNameFilter:
    """The server-side ``name`` query filter on list_projects."""

    def test_name_lands_in_query_when_set(self):
        """A non-blank name is sent as the upstream 'name' query param."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([{"key": "PROJ"}], is_last_page=True),
        ) as mock_get:
            fetcher.list_projects(name="Proj")

        assert mock_get.call_args[1]["params"]["name"] == "Proj"

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_name_absent_from_query_when_unset(self, name):
        """A None/blank name does not add a 'name' query param."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([{"key": "PROJ"}], is_last_page=True),
        ) as mock_get:
            fetcher.list_projects(name=name)

        assert "name" not in mock_get.call_args[1]["params"]

    def test_name_coexists_with_configured_projects_filter(self):
        """The server-side name and the client-side key allowlist both apply.

        The configured ``projects_filter`` keeps its capped walk and narrows the
        result client-side, while ``name`` rides as a server-side query param on
        every page request — the two filters are independent.
        """
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="PROJ"))
        pages = [
            _page_response([{"key": "OTHER"}], is_last_page=False, next_page_start=1),
            _page_response([{"key": "PROJ"}], is_last_page=True),
        ]
        with patch.object(fetcher._session, "get", side_effect=pages) as mock_get:
            page = fetcher.list_projects(name="Proj")

        # Server-side name param rides on every page of the capped walk.
        assert mock_get.call_count == 2
        assert all(
            call.kwargs["params"]["name"] == "Proj" for call in mock_get.call_args_list
        )
        # Client-side allowlist still narrows the returned models.
        assert [p.key for p in page.projects] == ["PROJ"]

    def test_name_filter_resumes_from_cursor(self):
        """A capped walk with name + projects_filter honours a non-zero start."""
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="PROJ"))
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([{"key": "PROJ"}], is_last_page=True),
        ) as mock_get:
            fetcher.list_projects(name="Proj", start=500)

        params = mock_get.call_args[1]["params"]
        assert params["start"] == 500
        assert params["name"] == "Proj"


def _http_error_response(status: int) -> MagicMock:
    """Build a mock response whose raise_for_status raises an HTTPError."""
    response = MagicMock()
    response.status_code = status
    error = HTTPError(f"{status} error")
    error.response = response
    response.raise_for_status.side_effect = error
    return response


class TestBitbucketFetcherErrorHandling:
    """The _get error surface: auth, server, and non-JSON-body failures."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_authentication_error(self, status):
        """A 401/403 surfaces as MCPAtlassianAuthenticationError."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(status)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match=str(status)):
                fetcher.list_projects()

    def test_server_error_raises_value_error(self):
        """A 5xx surfaces as a descriptive ValueError (not the auth error)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(503)
        ):
            with pytest.raises(ValueError, match="HTTP 503"):
                fetcher.list_projects()

    def test_not_found_status_raises_resource_not_found_error(self):
        """A 404 surfaces as the typed not-found error (a ValueError subclass).

        The typed exception lets tools render an actionable 'not found' message
        while staying caught by any generic ``except ValueError`` handler.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.list_projects()

        assert isinstance(excinfo.value, ValueError)
        message = str(excinfo.value)
        assert "not found" in message.lower()
        # Leak-free: cites the API path and an explanation, not the upstream body.
        assert "/projects" in message

    def test_html_body_on_200_raises_value_error(self):
        """A 200 with a non-JSON body (proxy login page) is a clear ValueError."""
        fetcher = BitbucketFetcher(config=_byo_config())
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.side_effect = json.JSONDecodeError("err", "<html>", 0)
        with patch.object(fetcher._session, "get", return_value=mock_response):
            with pytest.raises(ValueError, match="non-JSON response"):
                fetcher.list_projects()

    def test_connection_error_raises_value_error(self):
        """A connection failure surfaces as a descriptive ValueError."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            side_effect=RequestsConnectionError("refused"),
        ):
            with pytest.raises(ValueError, match="Could not connect"):
                fetcher.list_projects()

    def test_read_timeout_raises_leak_free_value_error(self):
        """A ReadTimeout surfaces as a ValueError without the urllib3 pool repr.

        ReadTimeout descends from Timeout, not ConnectionError, so it used to
        escape _get and leak the raw 'HTTPSConnectionPool(host=...)' string to
        the client. Regression guard for that leak.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        leaky = ReadTimeout(
            "HTTPSConnectionPool(host='bitbucket.corp.example.com', port=443): "
            "Read timed out. (read timeout=75)"
        )
        with patch.object(fetcher._session, "get", side_effect=leaky):
            with pytest.raises(ValueError) as excinfo:
                fetcher.list_projects()

        message = str(excinfo.value)
        assert "timed out" in message
        # The raw transport internals must not reach the client.
        assert "HTTPSConnectionPool" not in message
        assert "read timeout=75" not in message

    def test_ssl_error_raises_leak_free_value_error(self):
        """An SSLError surfaces as a distinct, leak-free ValueError.

        SSLError subclasses ConnectionError; the dedicated clause gives a
        certificate-specific message instead of the generic 'could not connect'
        one, and still strips the urllib3 pool repr.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        leaky = SSLError(
            "HTTPSConnectionPool(host='bitbucket.corp.example.com', port=443): "
            "certificate verify failed: self-signed certificate"
        )
        with patch.object(fetcher._session, "get", side_effect=leaky):
            with pytest.raises(ValueError) as excinfo:
                fetcher.list_projects()

        message = str(excinfo.value)
        assert "SSL verification failed" in message
        assert "HTTPSConnectionPool" not in message
        assert "certificate verify failed" not in message

    def test_other_transport_error_raises_leak_free_value_error(self):
        """Any other requests transport error is caught leak-free by the backstop.

        ChunkedEncodingError (and siblings) subclass RequestException but not the
        clauses above; without the backstop they fall through to the server's
        OSError branch, which interpolates str(e) and leaks the pool repr.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        leaky = ChunkedEncodingError(
            "HTTPSConnectionPool(host='bitbucket.corp.example.com', port=443): "
            "Response ended prematurely"
        )
        with patch.object(fetcher._session, "get", side_effect=leaky):
            with pytest.raises(ValueError) as excinfo:
                fetcher.list_projects()

        message = str(excinfo.value)
        assert "network error" in message
        assert "HTTPSConnectionPool" not in message
        assert "Response ended prematurely" not in message


def _http_error_with_body(status: int, body) -> MagicMock:
    """Build a mock error response that raises and whose .json() returns body."""
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body
    error = HTTPError(f"{status} error")
    error.response = response
    response.raise_for_status.side_effect = error
    return response


class TestRequestWriteTaxonomy:
    """The shared _request taxonomy as exercised through the _post wrapper.

    _get is proven byte-identical by the suite above; these pin the write-side
    behaviour _request newly carries: the JSON body is sent, and a 400/409
    surfaces the instance's own errors[].message (and nothing else) while the
    rest of the taxonomy (auth, not-found, timeout) is shared with _get.
    """

    def test_post_sends_json_body_to_the_path(self):
        """_post issues a POST with the JSON body to the API-root-joined URL."""
        fetcher = BitbucketFetcher(config=_byo_config())
        ok = MagicMock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"id": 1}
        with patch.object(fetcher._session, "post", return_value=ok) as mock_post:
            result = fetcher._post(
                "/projects/PROJ/repos/r/pull-requests/1/comments",
                json_body={"text": "hi"},
            )

        assert result == {"id": 1}
        called_url = mock_post.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/r/pull-requests/1/comments"
        )
        assert mock_post.call_args[1]["json"] == {"text": "hi"}

    def test_post_400_surfaces_envelope_messages(self):
        """A 400 with the documented envelope appends errors[].message text."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "Anchor 'path' is required."}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(400, body)
        ):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        message = str(excinfo.value)
        assert "HTTP 400" in message
        assert "Anchor 'path' is required." in message

    def test_post_409_joins_multiple_messages(self):
        """Multiple envelope messages are joined into one actionable string."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "first"}, {"message": "second"}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        message = str(excinfo.value)
        assert "HTTP 409" in message
        assert "first; second" in message

    def test_post_400_malformed_body_falls_back_to_generic(self):
        """A 400 whose body is not the documented envelope never echoes it."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_http_error_with_body(400, {"unexpected": "shape"}),
        ):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        message = str(excinfo.value)
        assert "HTTP 400" in message
        # The body is not the documented shape, so nothing from it is surfaced.
        assert "unexpected" not in message

    def test_post_400_non_json_body_falls_back_to_generic(self):
        """A 400 whose body fails to parse falls back without leaking it."""
        fetcher = BitbucketFetcher(config=_byo_config())
        resp = MagicMock()
        resp.status_code = 400
        resp.json.side_effect = json.JSONDecodeError("err", "<html>", 0)
        error = HTTPError("400 error")
        error.response = resp
        resp.raise_for_status.side_effect = error
        with patch.object(fetcher._session, "post", return_value=resp):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        message = str(excinfo.value)
        assert "HTTP 400" in message
        assert "<html>" not in message

    def test_post_400_empty_errors_list_falls_back_to_generic(self):
        """A well-formed envelope with no messages yields the generic message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_http_error_with_body(400, {"errors": []}),
        ):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        message = str(excinfo.value)
        # No detail to surface, so the status-only generic message is used.
        assert message.rstrip().endswith("HTTP 400.")

    def test_post_400_message_is_length_capped(self):
        """An over-long upstream message is bounded, not echoed in full."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "x" * 2000}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(400, body)
        ):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        message = str(excinfo.value)
        # The surfaced detail is capped (500 chars), so the full body is bounded.
        assert "x" * 500 in message
        assert "x" * 501 not in message

    @pytest.mark.parametrize("status", [401, 403])
    def test_post_auth_status_raises_authentication_error(self, status):
        """401/403 surface as the shared auth error for writes too."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_http_error_response(status)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match=str(status)):
                fetcher._post("/x", json_body={"text": "hi"})

    def test_post_404_raises_resource_not_found(self):
        """404 surfaces as the typed not-found error for writes too."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher._post("/x", json_body={"text": "hi"})

    def test_post_timeout_is_leak_free(self):
        """The timeout branch strips the urllib3 pool repr for POST too."""
        fetcher = BitbucketFetcher(config=_byo_config())
        leaky = ReadTimeout(
            "HTTPSConnectionPool(host='bitbucket.corp.example.com', port=443): "
            "Read timed out. (read timeout=75)"
        )
        with patch.object(fetcher._session, "post", side_effect=leaky):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        message = str(excinfo.value)
        assert "timed out" in message
        assert "HTTPSConnectionPool" not in message

    def test_put_sends_json_body(self):
        """_put issues a PUT with the JSON body to the API-root-joined URL."""
        fetcher = BitbucketFetcher(config=_byo_config())
        ok = MagicMock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"status": "APPROVED"}
        with patch.object(fetcher._session, "put", return_value=ok) as mock_put:
            result = fetcher._put(
                "/x/participants/me", json_body={"status": "APPROVED"}
            )

        assert result == {"status": "APPROVED"}
        assert mock_put.call_args[1]["json"] == {"status": "APPROVED"}


def _response_with_header(body, username):
    """A successful response carrying an X-AUSERNAME header and JSON body."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"X-AUSERNAME": username} if username else {}
    response.json.return_value = body
    return response


class TestCurrentUserResolution:
    """X-AUSERNAME capture and username→slug resolution for the write path."""

    def test_request_captures_ausername_header(self):
        """Any authenticated response populates the username cache."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_response_with_header({"count": 0}, "jdoe"),
        ):
            fetcher._get("/inbox/pull-requests/count")

        assert fetcher._auth_username == "jdoe"

    def test_absent_header_leaves_cache_untouched(self):
        """A response without the header does not corrupt the cache."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_response_with_header({"count": 0}, None),
        ):
            fetcher._get("/inbox/pull-requests/count")

        assert fetcher._auth_username is None

    @staticmethod
    def _directory(username, users):
        """A session.get that primes X-AUSERNAME then serves /users?filter."""

        def _get(url, params=None, timeout=None):
            if url.endswith("/inbox/pull-requests/count"):
                return _response_with_header({"count": 0}, username)
            if url.endswith("/users"):
                return _response_with_header(
                    {"values": users, "isLastPage": True}, username
                )
            return _response_with_header({}, username)

        return _get

    def test_resolves_slug_by_exact_name_match(self):
        """The username resolves to the user whose name matches exactly."""
        fetcher = BitbucketFetcher(config=_byo_config())
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session, "get", side_effect=self._directory("jdoe", users)
        ):
            assert fetcher._resolve_current_user_slug() == "jdoe-slug"

    def test_multi_result_picks_exact_name_not_prefix_sibling(self):
        """A prefix-matching sibling in the result set is never selected."""
        fetcher = BitbucketFetcher(config=_byo_config())
        users = [
            {"name": "jdoe2", "slug": "jdoe2-slug"},
            {"name": "jdoe", "slug": "jdoe-slug"},
        ]
        with patch.object(
            fetcher._session, "get", side_effect=self._directory("jdoe", users)
        ):
            assert fetcher._resolve_current_user_slug() == "jdoe-slug"

    def test_no_exact_match_raises(self):
        """No exact name match raises rather than guessing a slug."""
        fetcher = BitbucketFetcher(config=_byo_config())
        users = [{"name": "jdoeX", "slug": "x"}]
        with patch.object(
            fetcher._session, "get", side_effect=self._directory("jdoe", users)
        ):
            with pytest.raises(ValueError, match="no exact name match"):
                fetcher._resolve_current_user_slug()

    def test_missing_ausername_raises_before_lookup(self):
        """Without X-AUSERNAME the caller identity is unknown — raise clearly."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", side_effect=self._directory(None, [])
        ):
            with pytest.raises(ValueError, match="X-AUSERNAME"):
                fetcher._resolve_current_user_slug()

    def test_slug_is_cached_after_first_resolution(self):
        """The resolved slug is memoised; a second call makes no request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session, "get", side_effect=self._directory("jdoe", users)
        ) as mock_get:
            fetcher._resolve_current_user_slug()
            mock_get.reset_mock()
            assert fetcher._resolve_current_user_slug() == "jdoe-slug"
            mock_get.assert_not_called()

    def test_resolves_slug_on_a_later_page(self):
        """The exact match is found even when a prefix sibling fills page one."""
        fetcher = BitbucketFetcher(config=_byo_config())

        def _paged(url, params=None, timeout=None):
            if url.endswith("/inbox/pull-requests/count"):
                return _response_with_header({"count": 0}, "jdoe")
            # Page 1 carries only a prefix sibling; the exact match is on page 2.
            if (params or {}).get("start", 0) == 0:
                return _response_with_header(
                    {
                        "values": [{"name": "jdoer", "slug": "jdoer-slug"}],
                        "isLastPage": False,
                        "nextPageStart": 1,
                    },
                    "jdoe",
                )
            return _response_with_header(
                {
                    "values": [{"name": "jdoe", "slug": "jdoe-slug"}],
                    "isLastPage": True,
                },
                "jdoe",
            )

        with patch.object(fetcher._session, "get", side_effect=_paged):
            assert fetcher._resolve_current_user_slug() == "jdoe-slug"
