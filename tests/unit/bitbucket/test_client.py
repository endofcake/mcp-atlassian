"""Unit tests for BitbucketFetcher."""

import json
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError
from requests.exceptions import ChunkedEncodingError, ReadTimeout, SSLError
from requests.exceptions import ConnectionError as RequestsConnectionError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.client import (
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
        """list_projects unwraps the paged 'values' field on a single page."""
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
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects")
        # The endpoint is paged with start/limit, not a single bare limit.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 100}


class TestListProjectsPagination:
    """list_projects pagination, truncation, and response-shape validation."""

    def test_walks_pages_until_last_page(self):
        """Pages are followed via nextPageStart and concatenated."""
        fetcher = BitbucketFetcher(config=_byo_config())
        pages = [
            _page_response(
                [{"key": "A"}, {"key": "B"}], is_last_page=False, next_page_start=2
            ),
            _page_response([{"key": "C"}], is_last_page=True),
        ]
        with patch.object(fetcher._session, "get", side_effect=pages) as mock_get:
            page = fetcher.list_projects(limit=25)

        assert [p.key for p in page.projects] == ["A", "B", "C"]
        assert page.is_last_page is True
        assert page.truncated is False
        # Second request used the advertised cursor.
        assert mock_get.call_args_list[0][1]["params"]["start"] == 0
        assert mock_get.call_args_list[1][1]["params"]["start"] == 2

    def test_truncates_at_limit_without_overfetching(self):
        """A small limit against a large org stops after one page, flags truncation."""
        fetcher = BitbucketFetcher(config=_byo_config())
        big_page = _page_response(
            [{"key": f"P{i}"} for i in range(100)],
            is_last_page=False,
            next_page_start=100,
        )
        with patch.object(fetcher._session, "get", return_value=big_page) as mock_get:
            page = fetcher.list_projects(limit=25)

        assert len(page.projects) == 25
        assert page.truncated is True
        assert page.is_last_page is False
        # Collected 100 >= limit on the first page, so no second request.
        assert mock_get.call_count == 1

    def test_last_page_overshooting_limit_is_truncated(self):
        """A complete final page larger than limit trims and flags truncation.

        The common default-limit case (small org, one page of >limit projects):
        is_last_page stays True (no more pages to fetch) while truncated is True
        (the result was trimmed), telling the caller to raise the limit.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        single_page = _page_response(
            [{"key": f"P{i}"} for i in range(60)], is_last_page=True
        )
        with patch.object(
            fetcher._session, "get", return_value=single_page
        ) as mock_get:
            page = fetcher.list_projects(limit=25)

        assert len(page.projects) == 25
        assert page.is_last_page is True
        assert page.truncated is True
        # No nextPageStart was followed; the single page was upstream-final.
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

    def test_limit_clamped_to_max_and_capped(self):
        """A limit above the cap is clamped; a huge org truncates at MAX_PROJECTS_LIMIT."""
        fetcher = BitbucketFetcher(config=_byo_config())

        def endless(url, params=None, timeout=None):
            start = params["start"]
            return _page_response(
                [{"key": f"P{start + i}"} for i in range(100)],
                is_last_page=False,
                next_page_start=start + 100,
            )

        with patch.object(fetcher._session, "get", side_effect=endless):
            page = fetcher.list_projects(limit=10_000)

        assert len(page.projects) == MAX_PROJECTS_LIMIT
        assert page.truncated is True
        assert page.is_last_page is False

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
        """Pagination continues past non-matching pages to find filtered keys."""
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="C"))
        pages = [
            _page_response(
                [{"key": "A"}, {"key": "B"}], is_last_page=False, next_page_start=2
            ),
            _page_response([{"key": "C"}], is_last_page=True),
        ]
        with patch.object(fetcher._session, "get", side_effect=pages):
            page = fetcher.list_projects()

        assert [p.key for p in page.projects] == ["C"]
        assert page.is_last_page is True
        assert page.truncated is False

    def test_blank_filter_returns_all_projects(self):
        """An empty/whitespace filter is treated as no filter."""
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="  "))
        body = _page_response([{"key": "PROJ"}, {"key": "OTHER"}], is_last_page=True)
        with patch.object(fetcher._session, "get", return_value=body):
            page = fetcher.list_projects()

        assert [p.key for p in page.projects] == ["PROJ", "OTHER"]


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
