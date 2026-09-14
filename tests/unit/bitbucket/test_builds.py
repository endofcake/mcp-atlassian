"""Unit tests for BuildsMixin (commit build statuses)."""

from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.builds import MAX_BUILD_STATUSES_LIMIT
from mcp_atlassian.bitbucket.client import BitbucketResourceNotFoundError
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketBuildStatus
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import attach_json

FULL_SHA = "e00cf62997a027bbf785614a93e2e55bb331d268"


def _status(key, state, *, test_results=None):
    """Build a minimal RestBuildStatus-shaped dict."""
    body = {
        "key": key,
        "name": f"Plan {key}",
        "state": state,
        "url": f"https://ci.corp.example.com/browse/{key}",
        "description": "A build",
        "buildNumber": "3",
        "duration": 1500,
        "ref": "refs/heads/main",
        "parent": "PLAN",
        "projectKey": "PROJ",
        "repositorySlug": "my-repo",
        "createdDate": 1700000000000,
        "updatedDate": 1700000100000,
    }
    if test_results is not None:
        body["testResults"] = test_results
    return body


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of the build-status endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    attach_json(response, body)
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
    """Build a DC BYO-token fetcher for build-status tests."""
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


class TestGetCommitBuildStatuses:
    """Happy-path listing, model mapping, and single-window pagination."""

    def test_returns_build_status_models_under_build_status_module(self):
        """The call hits the build-status module and maps every state."""
        fetcher = _fetcher()
        values = [
            _status("OK", "SUCCESSFUL", test_results={"successful": 10}),
            _status("BAD", "FAILED", test_results={"failed": 2, "successful": 8}),
            _status("RUN", "INPROGRESS"),
        ]
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(values, is_last_page=True),
        ) as mock_get:
            page = fetcher.get_commit_build_statuses(FULL_SHA)

        assert mock_get.call_count == 1
        called_url = mock_get.call_args[0][0]
        assert called_url == (
            "https://bitbucket.corp.example.com/rest/build-status/1.0/commits/"
            f"{FULL_SHA}"
        )
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 25}
        assert all(isinstance(s, BitbucketBuildStatus) for s in page.statuses)
        assert [s.state for s in page.statuses] == [
            "SUCCESSFUL",
            "FAILED",
            "INPROGRESS",
        ]
        assert page.statuses[0].test_results is not None
        assert page.statuses[0].test_results.successful == 10
        assert page.statuses[2].test_results is None
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

    def test_page_counts_cover_this_window_only(self):
        """page_counts tallies the returned window, not the whole commit."""
        fetcher = _fetcher()
        values = [
            _status("A", "SUCCESSFUL"),
            _status("B", "SUCCESSFUL"),
            _status("C", "FAILED"),
        ]
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(values, is_last_page=False, next_page_start=3),
        ):
            page = fetcher.get_commit_build_statuses(FULL_SHA, limit=3)

        assert page.page_counts == {"SUCCESSFUL": 2, "FAILED": 1}
        assert page.truncated is True
        assert page.next_page_start == 3

    def test_resumed_final_page_counts_only_its_window(self):
        """A final page reached by cursor tallies its window, not earlier pages.

        is_last_page true and truncated false on a resumed page must not be
        read as whole-commit coverage. The earlier pages are not re-read.
        """
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_status("Z", "SUCCESSFUL")], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.get_commit_build_statuses(FULL_SHA, start=25)

        assert mock_get.call_count == 1
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.page_counts == {"SUCCESSFUL": 1}

    def test_status_without_state_counts_as_unknown(self):
        """A status missing its state is tallied under UNKNOWN."""
        fetcher = _fetcher()
        body = _status("A", "SUCCESSFUL")
        del body["state"]
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([body], is_last_page=True),
        ):
            page = fetcher.get_commit_build_statuses(FULL_SHA)

        assert page.page_counts == {"UNKNOWN": 1}

    def test_start_resumes_from_cursor(self):
        """A resumed call sends the cursor as start."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.get_commit_build_statuses(FULL_SHA, start=25)

        assert mock_get.call_args[1]["params"] == {"start": 25, "limit": 25}

    def test_commit_id_is_stripped_and_lower_cased(self):
        """Surrounding whitespace is dropped and the SHA is sent lower-case."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.get_commit_build_statuses(f"  {FULL_SHA.upper()}  ")

        assert mock_get.call_args[0][0].endswith(f"/commits/{FULL_SHA}")

    def test_empty_page_reports_no_statuses(self):
        """A commit with no builds yields an empty, complete window."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ):
            page = fetcher.get_commit_build_statuses(FULL_SHA)

        assert page.statuses == []
        assert page.page_counts == {}
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

    def test_order_by_is_normalized_into_params(self):
        """order_by is case-normalized to the endpoint's enum."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.get_commit_build_statuses(FULL_SHA, order_by="status")

        assert mock_get.call_args[1]["params"]["orderBy"] == "STATUS"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_order_by_is_dropped(self, blank):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.get_commit_build_statuses(FULL_SHA, order_by=blank)

        assert "orderBy" not in mock_get.call_args[1]["params"]

    def test_unknown_order_by_raises_without_request(self):
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="order_by must be one of"):
                fetcher.get_commit_build_statuses(FULL_SHA, order_by="RANDOM")
        mock_get.assert_not_called()

    def test_limit_clamped_to_max(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.get_commit_build_statuses(FULL_SHA, limit=5000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_BUILD_STATUSES_LIMIT

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",
            "   ",
            "abc",  # too short
            "e00cf62",  # abbreviated: the endpoint cannot resolve it
            "a" * 39,  # one short of a full SHA
            "g" * 40,  # not hex
            "a" * 41,  # too long
            "refs/heads/main",
            "..",
            "abc1234?x=1",
        ],
    )
    def test_non_hex_commit_id_raises_without_request(self, bad_id):
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="commit_id must be a full commit"):
                fetcher.get_commit_build_statuses(bad_id)
        mock_get.assert_not_called()

    def test_non_list_values_raises_value_error(self):
        """A page whose values is not a list is a response-shape error."""
        fetcher = _fetcher()
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, {"values": {"key": "A"}, "isLastPage": True})
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_commit_build_statuses(FULL_SHA)


class TestGetCommitBuildStatusesErrors:
    """The shared error taxonomy applies to the build-status module."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_authentication_error(self, status):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(status)
        ):
            with pytest.raises(MCPAtlassianAuthenticationError):
                fetcher.get_commit_build_statuses(FULL_SHA)

    def test_not_found_names_the_missing_module(self):
        """A 404 is reported as the build-status module being absent."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(
                BitbucketResourceNotFoundError,
                match="build-status module not found",
            ) as exc_info:
                fetcher.get_commit_build_statuses(FULL_SHA)

        message = str(exc_info.value)
        assert f"/rest/build-status/1.0/commits/{FULL_SHA}" in message
        assert "does not exist" not in message

    def test_server_error_raises_value_error(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(500)
        ):
            with pytest.raises(ValueError):
                fetcher.get_commit_build_statuses(FULL_SHA)
