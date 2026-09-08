"""Unit tests for BitbucketFetcher."""

import json
import logging
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError
from requests.exceptions import ChunkedEncodingError, ReadTimeout, SSLError
from requests.exceptions import ConnectionError as RequestsConnectionError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.client import (
    _MAX_USER_LOOKUP_PAGES,
    DEFAULT_MAX_RESPONSE_BYTES,
    MAX_PROJECTS_LIMIT,
    BitbucketClient,
    BitbucketResourceNotFoundError,
    BitbucketResponseTooLargeError,
)
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import (
    attach_body,
    attach_json,
    json_response,
)


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of the projects endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    attach_json(response, body)
    return response


def _byo_config(
    token: str = "user-bearer-token",
    projects_filter: str | None = None,
    **overrides: Any,
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
        **overrides,
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

    def test_ssl_verification_configured_for_the_service(self):
        """The session SSL setup receives the service name, URL and flag."""
        with patch(
            "mcp_atlassian.bitbucket.client.configure_ssl_verification"
        ) as mock_ssl:
            fetcher = BitbucketFetcher(config=_byo_config(ssl_verify=False))

        mock_ssl.assert_called_once_with(
            service_name="Bitbucket",
            url="https://bitbucket.corp.example.com",
            session=fetcher._session,
            ssl_verify=False,
        )

    def test_proxies_applied_to_session_and_no_proxy_env(self, monkeypatch):
        """Configured proxies reach the session; NO_PROXY reaches the env."""
        # setenv (not delenv) so monkeypatch records an undo entry, since the
        # shared proxy helper writes os.environ["NO_PROXY"] during construction.
        monkeypatch.setenv("NO_PROXY", "")

        fetcher = BitbucketFetcher(
            config=_byo_config(
                http_proxy="http://proxy.corp.example.com:8080",
                https_proxy="https://proxy.corp.example.com:8443",
                socks_proxy="socks5://proxy.corp.example.com:1080",
                no_proxy="localhost,.corp.example.com",
            )
        )

        assert fetcher._session.proxies == {
            "http": "http://proxy.corp.example.com:8080",
            "https": "https://proxy.corp.example.com:8443",
            "socks": "socks5://proxy.corp.example.com:1080",
        }
        assert os.environ["NO_PROXY"] == "localhost,.corp.example.com"

    def test_without_proxy_config_session_proxies_stay_empty(self):
        fetcher = BitbucketFetcher(config=_byo_config())

        assert fetcher._session.proxies == {}

    def test_custom_headers_applied_to_session(self):
        fetcher = BitbucketFetcher(
            config=_byo_config(custom_headers={"X-Corp-Header": "yes"})
        )

        assert fetcher._session.headers["X-Corp-Header"] == "yes"

    def test_custom_header_names_logged_without_values(self, caplog):
        """Debug logs name each applied header but never carry its value."""
        caplog.set_level(logging.DEBUG, logger="mcp-atlassian.bitbucket")

        BitbucketFetcher(
            config=_byo_config(
                custom_headers={"X-Corp-Header": "corp-42", "X-ALB-Token": "s3cret"}
            )
        )

        assert "Applied custom header: X-Corp-Header" in caplog.text
        assert "Applied custom header: X-ALB-Token" in caplog.text
        assert "s3cret" not in caplog.text
        assert "corp-42" not in caplog.text

    def test_default_user_agent_set_on_session(self):
        """The session carries the package User-Agent in place of the requests one."""
        fetcher = BitbucketFetcher(config=_byo_config())

        assert fetcher._session.headers["User-Agent"].startswith("mcp-atlassian/")

    def test_custom_headers_can_override_user_agent(self):
        fetcher = BitbucketFetcher(
            config=_byo_config(custom_headers={"User-Agent": "corp-scanner/1.0"})
        )

        assert fetcher._session.headers["User-Agent"] == "corp-scanner/1.0"

    def test_ssrf_redirect_hook_attached(self):
        """The session validates redirect targets on every response."""
        fetcher = BitbucketFetcher(config=_byo_config())

        assert len(fetcher._session.hooks["response"]) == 1

    def test_ssrf_pinning_adapter_mounted(self):
        """DNS-pinning adapters replace the default http/https adapters."""
        fetcher = BitbucketFetcher(config=_byo_config())

        for scheme in ("http://", "https://"):
            assert type(fetcher._session.adapters[scheme]).__name__ == (
                "SsrfPinningAdapter"
            )

    def test_http_hardening_configured_for_the_service(self):
        """The opt-in retry/concurrency/rate-limit/breaker knobs are wired."""
        with (
            patch("mcp_atlassian.bitbucket.client.configure_retry") as mock_retry,
            patch(
                "mcp_atlassian.bitbucket.client.configure_concurrency"
            ) as mock_concurrency,
            patch(
                "mcp_atlassian.bitbucket.client.configure_rate_limit"
            ) as mock_rate_limit,
            patch(
                "mcp_atlassian.bitbucket.client.configure_circuit_breaker"
            ) as mock_breaker,
        ):
            fetcher = BitbucketFetcher(config=_byo_config())

        for mock in (mock_retry, mock_concurrency, mock_rate_limit, mock_breaker):
            mock.assert_called_once_with(fetcher._session, service="Bitbucket")

    def test_failed_oauth_session_configuration_raises(self):
        """An OAuth session that cannot be configured fails construction."""
        with (
            patch(
                "mcp_atlassian.bitbucket.client.configure_oauth_session",
                return_value=False,
            ),
            pytest.raises(
                MCPAtlassianAuthenticationError,
                match="Failed to configure Bitbucket OAuth session",
            ),
        ):
            BitbucketFetcher(config=_byo_config())


class TestBitbucketFetcherCalls:
    """Tests for the validation and list calls."""

    def test_get_current_user_hits_validation_endpoint(self):
        """get_current_user issues a GET against the inbox count endpoint."""
        fetcher = BitbucketFetcher(config=_byo_config())

        mock_response = MagicMock()
        attach_json(mock_response, {"count": 0})
        with patch.object(
            fetcher._session, "get", return_value=mock_response
        ) as mock_get:
            result = fetcher.get_current_user()

        assert result == {"count": 0}
        called_url = mock_get.call_args[0][0]
        assert called_url == (
            "https://bitbucket.corp.example.com/rest/api/1.0/inbox/pull-requests/count"
        )

    def test_get_current_user_rejects_non_dict_response(self):
        """A non-object validation body is an auth error."""
        fetcher = BitbucketFetcher(config=_byo_config())
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, ["not", "a", "dict"])

        with (
            patch.object(fetcher._session, "get", return_value=response),
            pytest.raises(
                MCPAtlassianAuthenticationError,
                match="Unexpected Bitbucket validation response",
            ),
        ):
            fetcher.get_current_user()

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

        # list_projects returns BitbucketProject models.
        assert [p.key for p in page.projects] == ["PROJ", "TEAM"]
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/projects")
        # Unfiltered: single window, and the page size sent upstream is the limit.
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

    def test_global_pagination_ceiling_clamps_limit(self, monkeypatch):
        """The opt-in ATLASSIAN_MAX_PAGINATION_LIMIT ceiling caps the window."""
        monkeypatch.setenv("ATLASSIAN_MAX_PAGINATION_LIMIT", "2")
        fetcher = BitbucketFetcher(config=_byo_config())
        values = [{"key": f"P{i}", "name": f"p{i}"} for i in range(3)]
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(values[:2], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_projects(limit=10)

        assert mock_get.call_args[1]["params"]["limit"] == 2
        assert len(page.projects) == 2

    def test_non_dict_response_raises_value_error(self):
        """A non-paged body (a bare list) raises an error."""
        fetcher = BitbucketFetcher(config=_byo_config())
        bad = MagicMock()
        bad.raise_for_status.return_value = None
        attach_json(bad, ["not", "a", "page"])
        with patch.object(fetcher._session, "get", return_value=bad):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.list_projects()

    def test_missing_values_field_raises_value_error(self):
        """A dict without a 'values' list raises an error."""
        fetcher = BitbucketFetcher(config=_byo_config())
        bad = MagicMock()
        bad.raise_for_status.return_value = None
        attach_json(bad, {"size": 0, "isLastPage": True})
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

    def test_filtered_empty_window_returns_cursor_without_walking(self):
        """A window the allowlist empties issues ONE request and hands back the
        upstream cursor.

        The load-safety contract: the server does not walk further pages to fill
        a filtered window. The caller resumes with ``start=next_page_start``.
        """
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="C"))
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [{"key": "A"}, {"key": "B"}], is_last_page=False, next_page_start=2
            ),
        ) as mock_get:
            page = fetcher.list_projects()

        assert mock_get.call_count == 1
        assert page.projects == []
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 2

    def test_filtered_resume_finds_match_on_next_window(self):
        """Resuming from the returned cursor reaches the matching later window."""
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="C"))
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([{"key": "C"}], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_projects(start=2)

        assert mock_get.call_count == 1
        assert mock_get.call_args[1]["params"]["start"] == 2
        assert [p.key for p in page.projects] == ["C"]
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

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

        The configured ``projects_filter`` narrows the fetched window
        client-side, while ``name`` rides as a server-side query param on the
        request, so the two filters are independent.
        """
        fetcher = BitbucketFetcher(config=_byo_config(projects_filter="PROJ"))
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [{"key": "OTHER"}, {"key": "PROJ"}], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.list_projects(name="Proj")

        assert mock_get.call_count == 1
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

    def test_rate_limit_status_surfaces_retry_after_hint(self):
        """A 429 names the rate limit and the server's Retry-After value."""
        fetcher = BitbucketFetcher(config=_byo_config())
        response = _http_error_response(429)
        response.headers = {"Retry-After": "30"}
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(ValueError, match="rate limit") as excinfo:
                fetcher.list_projects()

        assert "30 seconds" in str(excinfo.value)

    def test_rate_limit_status_without_retry_after_still_says_back_off(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        response = _http_error_response(429)
        response.headers = {}
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(ValueError, match="No Retry-After header"):
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
        # Leak-free: cites the API path and an explanation and omits the upstream body.
        assert "/projects" in message

    def test_html_body_on_200_raises_value_error(self):
        """A 200 with a non-JSON body (proxy login page) is a clear ValueError."""
        fetcher = BitbucketFetcher(config=_byo_config())
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        attach_body(mock_response, b"<html>login</html>")
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
    attach_json(response, body)
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
        attach_json(ok, {"id": 1})
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
        """A 400 whose body is not the documented envelope does not echo it."""
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
        """An over-long upstream message is bounded."""
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
        attach_json(ok, {"status": "APPROVED"})
        with patch.object(fetcher._session, "put", return_value=ok) as mock_put:
            result = fetcher._put(
                "/x/participants/me", json_body={"status": "APPROVED"}
            )

        assert result == {"status": "APPROVED"}
        assert mock_put.call_args[1]["json"] == {"status": "APPROVED"}


def _no_content_response() -> MagicMock:
    """A 204 success: empty body, .json() would raise if ever called."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.side_effect = json.JSONDecodeError("no body", "", 0)
    return attach_body(response, b"")


class TestDeleteVerb:
    """_delete: query params, the 204/empty-body path, and the shared taxonomy.

    The error taxonomy is proven once via _get/_post above; these pin that
    _delete routes through the same _request ladder (so 401/404/409 map
    identically) and that an empty 204 success returns cleanly rather than
    raising the non-JSON-body path.
    """

    def test_sends_delete_with_query_params(self):
        """_delete issues a DELETE with the params to the API-root-joined URL."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_no_content_response()
        ) as mock_delete:
            result = fetcher._delete(
                "/projects/PROJ/repos/r/pull-requests/1/comments/9",
                params={"version": 2},
            )

        assert result is None
        called_url = mock_delete.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/r/pull-requests/1/comments/9"
        )
        assert mock_delete.call_args[1]["params"] == {"version": 2}

    def test_empty_204_body_returns_none_without_json_error(self):
        """A 204 with no body returns None without the non-JSON-body ValueError."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_no_content_response()
        ):
            # Would raise "non-JSON response" if the empty body were parsed.
            assert fetcher._delete("/x", params={"version": 0}) is None

    def test_whitespace_only_body_is_tolerated_as_empty(self):
        """A 2xx whose body is only whitespace is treated as no-content."""
        fetcher = BitbucketFetcher(config=_byo_config())
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = json.JSONDecodeError("ws", "  \n", 0)
        attach_body(resp, b"  \n")
        with patch.object(fetcher._session, "delete", return_value=resp):
            assert fetcher._delete("/x", params={"version": 1}) is None

    def test_409_has_replies_surfaces_envelope_message(self):
        """A 409 (stale version or has-replies) surfaces the instance's message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [{"message": "The comment has replies and cannot be deleted."}]
        }
        with patch.object(
            fetcher._session, "delete", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError) as excinfo:
                fetcher._delete("/x", params={"version": 1})

        message = str(excinfo.value)
        assert "HTTP 409" in message
        assert "has replies" in message

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_authentication_error(self, status):
        """401/403 surface as the shared auth error for DELETE too."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_http_error_response(status)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match=str(status)):
                fetcher._delete("/x", params={"version": 1})

    def test_404_raises_resource_not_found(self):
        """404 surfaces as the typed not-found error for DELETE too."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher._delete("/x", params={"version": 1})


def _response_with_header(body, username):
    """A successful response carrying an X-AUSERNAME header and JSON body."""
    response = json_response(body)
    if username:
        response.headers["X-AUSERNAME"] = username
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

        def _get(url, params=None, timeout=None, **kwargs):
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
        """A prefix-matching sibling in the result set is not selected."""
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
        """Without X-AUSERNAME the caller identity is unknown, so the lookup raises."""
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

    def test_string_is_last_page_in_users_page_raises(self):
        """A non-boolean isLastPage on the users page is a shape error."""
        fetcher = BitbucketFetcher(config=_byo_config())

        def _bad_flag(url, params=None, timeout=None, **kwargs):
            if url.endswith("/inbox/pull-requests/count"):
                return _response_with_header({"count": 0}, "jdoe")
            return _response_with_header(
                {"values": [{"name": "x", "slug": "x"}], "isLastPage": "false"},
                "jdoe",
            )

        with patch.object(fetcher._session, "get", side_effect=_bad_flag):
            with pytest.raises(ValueError, match="boolean 'isLastPage'"):
                fetcher._resolve_current_user_slug()

    def test_non_advancing_users_cursor_stops_after_one_page(self):
        """A cursor that does not advance ends the walk after one /users GET."""
        fetcher = BitbucketFetcher(config=_byo_config())

        def _stuck(url, params=None, timeout=None, **kwargs):
            if url.endswith("/inbox/pull-requests/count"):
                return _response_with_header({"count": 0}, "jdoe")
            return _response_with_header(
                {
                    "values": [{"name": "jdoer", "slug": "jdoer-slug"}],
                    "isLastPage": False,
                    "nextPageStart": 0,
                },
                "jdoe",
            )

        with patch.object(fetcher._session, "get", side_effect=_stuck) as mock_get:
            with pytest.raises(ValueError, match="no exact name match"):
                fetcher._resolve_current_user_slug()

        users_calls = [c for c in mock_get.call_args_list if c[0][0].endswith("/users")]
        assert len(users_calls) == 1

    def test_forever_advancing_users_cursor_stops_at_the_page_cap(self):
        """A directory that keeps paging is walked at most the capped number
        of times, then the lookup fails rather than scanning further."""
        fetcher = BitbucketFetcher(config=_byo_config())

        def _endless(url, params=None, timeout=None, **kwargs):
            if url.endswith("/inbox/pull-requests/count"):
                return _response_with_header({"count": 0}, "jdoe")
            start = (params or {}).get("start", 0)
            return _response_with_header(
                {
                    "values": [{"name": f"jdoe{start}", "slug": f"s{start}"}],
                    "isLastPage": False,
                    "nextPageStart": start + 1,
                },
                "jdoe",
            )

        with patch.object(fetcher._session, "get", side_effect=_endless) as mock_get:
            with pytest.raises(ValueError, match="no exact name match"):
                fetcher._resolve_current_user_slug()

        users_calls = [c for c in mock_get.call_args_list if c[0][0].endswith("/users")]
        assert len(users_calls) == _MAX_USER_LOOKUP_PAGES == 5
        assert [c[1]["params"]["start"] for c in users_calls] == [0, 1, 2, 3, 4]

    def test_resolves_slug_on_a_later_page(self):
        """The exact match is found even when a prefix sibling fills page one."""
        fetcher = BitbucketFetcher(config=_byo_config())

        def _paged(url, params=None, timeout=None, **kwargs):
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


class TestDefaultResponseCap:
    """Every request streams its body under the default byte cap."""

    @pytest.mark.parametrize("verb", ["get", "post", "put", "delete"])
    def test_every_verb_streams(self, verb):
        fetcher = BitbucketFetcher(config=_byo_config())
        response = MagicMock()
        response.raise_for_status.return_value = None
        # A DELETE success carries no body; the other verbs answer with JSON.
        attach_json(response, {} if verb == "delete" else {"ok": True})
        with patch.object(fetcher._session, verb, return_value=response) as mock_verb:
            if verb == "get":
                fetcher._get("/x")
            elif verb == "delete":
                fetcher._delete("/x")
            else:
                getattr(fetcher, f"_{verb}")("/x", json_body={})

        assert mock_verb.call_args[1]["stream"] is True

    def test_declared_length_over_default_cap_raises(self):
        """A plain GET with no explicit cap is still bounded by the default."""
        fetcher = BitbucketFetcher(config=_byo_config())
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Length": str(DEFAULT_MAX_RESPONSE_BYTES + 1)}
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(BitbucketResponseTooLargeError, match="10 MiB"):
                fetcher._get("/projects")

        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_explicit_cap_overrides_default(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Length": str(1024 + 1)}
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(BitbucketResponseTooLargeError):
                fetcher._get("/projects", max_response_bytes=1024)

    def test_chunked_body_over_default_cap_aborts(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        chunk = b"x" * 65536
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {}
        response.iter_content.return_value = iter(
            [chunk] * (DEFAULT_MAX_RESPONSE_BYTES // len(chunk) + 2)
        )
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(BitbucketResponseTooLargeError):
                fetcher._get("/projects")

        response.close.assert_called_once()


class TestResponseRelease:
    """Every response is closed, and error bodies are read under a cap."""

    def test_error_response_is_closed(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        response = _http_error_response(500)
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(ValueError):
                fetcher._get("/x")
        response.close.assert_called_once()

    def test_success_response_is_closed_after_read(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, {"ok": True})
        with patch.object(fetcher._session, "get", return_value=response):
            assert fetcher._get("/x") == {"ok": True}
        response.close.assert_called_once()

    def test_oversized_error_body_falls_back_to_generic_message(self):
        """A 400 whose body exceeds the error-body cap is not read in full."""
        fetcher = BitbucketFetcher(config=_byo_config())
        resp = MagicMock()
        resp.status_code = 400
        resp.headers = {"Content-Length": str(10 * 1024 * 1024)}
        error = HTTPError("400 error")
        error.response = resp
        resp.raise_for_status.side_effect = error
        with patch.object(fetcher._session, "post", return_value=resp):
            with pytest.raises(ValueError) as excinfo:
                fetcher._post("/x", json_body={"text": "hi"})

        assert str(excinfo.value).endswith("failed with HTTP 400.")
        resp.iter_content.assert_not_called()


class TestPathSegmentValidation:
    """Caller-supplied path segments cannot be blank or dot segments.

    ``quote(safe="")`` leaves periods unencoded, and the HTTP layer normalises
    ``.``/``..`` away, so an unrejected dot segment would silently move the
    request to a different endpoint (``/projects/../repos`` becomes the
    cross-project ``/repos``). Every segment goes through ``_encode_segment``,
    which raises before any request is issued.
    """

    @pytest.mark.parametrize("value", ["", "   ", ".", "..", " .. "])
    def test_encode_segment_rejects_blank_and_dot_segments(self, value):
        with pytest.raises(ValueError, match="project_key must"):
            BitbucketClient._encode_segment(
                value, name="project_key", what="project key"
            )

    def test_encode_segment_percent_encodes_survivors(self):
        encoded = BitbucketClient._encode_segment(
            " a/b?c ", name="project_key", what="project key"
        )
        assert encoded == "a%2Fb%3Fc"

    @pytest.mark.parametrize("key,slug", [("..", "r"), ("PROJ", ".."), (".", "r")])
    def test_repo_base_path_rejects_dot_segments(self, key, slug):
        with pytest.raises(ValueError, match="not the path segment"):
            BitbucketClient._repo_base_path(key, slug)


class TestInvalidUtf8Body:
    """A 2xx body that is not UTF-8 maps to the crafted non-JSON error."""

    def test_invalid_utf8_on_200_raises_non_json_value_error(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        attach_body(mock_response, b"\xff\xfe not json")
        with patch.object(fetcher._session, "get", return_value=mock_response):
            with pytest.raises(ValueError, match="non-JSON response"):
                fetcher.list_projects()
