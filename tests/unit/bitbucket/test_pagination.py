"""Unit tests for the shared BitbucketClient._fetch_page helper."""

from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.bitbucket import BitbucketConfig
from mcp_atlassian.bitbucket.client import BitbucketClient
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import attach_json


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of a Bitbucket paged endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    attach_json(response, body)
    return response


def _client() -> BitbucketClient:
    """Build a DC BYO-token base client for pagination tests."""
    return BitbucketClient(
        config=BitbucketConfig(
            url="https://bitbucket.corp.example.com",
            auth_type="oauth",
            oauth_config=BYOAccessTokenOAuthConfig(
                access_token="user-bearer-token",
                base_url="https://bitbucket.corp.example.com",
            ),
        )
    )


class TestFetchPageWindow:
    """One upstream request per call; window and completeness semantics."""

    def test_last_page_is_complete(self):
        """A last page returns its values, complete and untruncated."""
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response([{"id": 1}, {"id": 2}], is_last_page=True),
        ) as mock_get:
            page = client._fetch_page("/x", limit=25)

        assert page.values == [{"id": 1}, {"id": 2}]
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 25}

    def test_issues_exactly_one_request(self):
        """A non-last page triggers no follow-up request. The caller resumes
        with the returned cursor instead of the server walking."""
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response(
                [{"id": i} for i in range(25)], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = client._fetch_page("/x", limit=25)

        assert mock_get.call_count == 1
        assert len(page.values) == 25
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 25

    def test_last_page_overshooting_limit_is_truncated(self):
        """A server answering with more items than requested trims and flags
        truncation, with no forward cursor (the tail lives in this window)."""
        client = _client()
        single_page = _page_response([{"id": i} for i in range(60)], is_last_page=True)
        with patch.object(client._session, "get", return_value=single_page) as mock_get:
            page = client._fetch_page("/x", limit=25)

        assert len(page.values) == 25
        assert page.is_last_page is True
        assert page.truncated is True
        assert mock_get.call_count == 1
        assert page.next_page_start is None

    def test_non_last_page_overshooting_limit_has_no_cursor(self):
        """An overshot non-last page keeps its truncation flag and drops the
        upstream cursor, since the trimmed tail is unreachable forward."""
        client = _client()
        page_response = _page_response(
            [{"id": i} for i in range(30)], is_last_page=False, next_page_start=30
        )
        with patch.object(client._session, "get", return_value=page_response):
            page = client._fetch_page("/x", limit=25)

        assert len(page.values) == 25
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start is None

    def test_non_last_page_without_cursor_is_not_resumable(self):
        """isLastPage false but no nextPageStart: incomplete and unresumable.

        A DC instance can answer with ``isLastPage: false`` yet omit
        ``nextPageStart``. The result must signal truncation without a cursor so
        the caller does not treat it as complete or resume from a bad offset.
        """
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response([{"id": 1}], is_last_page=False),
        ) as mock_get:
            page = client._fetch_page("/x", limit=25)

        assert page.values == [{"id": 1}]
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start is None
        assert mock_get.call_count == 1

    def test_empty_last_page_is_not_truncated(self):
        """An empty, last page yields an empty, complete result (not an error)."""
        client = _client()
        with patch.object(
            client._session, "get", return_value=_page_response([], is_last_page=True)
        ):
            page = client._fetch_page("/x", limit=25)

        assert page.values == []
        assert page.is_last_page is True
        assert page.truncated is False


class TestFetchPageCursor:
    """The resume cursor: start passthrough and cursor validation."""

    def test_start_is_passed_through_to_request(self):
        """The start argument becomes the request's start query param."""
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response([{"id": 7}], is_last_page=True),
        ) as mock_get:
            client._fetch_page("/x", limit=25, start=50)

        assert mock_get.call_args[1]["params"] == {"start": 50, "limit": 25}

    def test_envelope_cursor_is_surfaced_verbatim(self):
        """A full non-last window returns the upstream nextPageStart verbatim."""
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response(
                [{"id": i} for i in range(25)], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = client._fetch_page("/x", limit=25)

        assert mock_get.call_count == 1
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 25

    def test_last_page_has_no_cursor(self):
        """A window that is the last page reports no resume cursor."""
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response([{"id": 1}], is_last_page=True),
        ):
            page = client._fetch_page("/x", limit=25)

        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

    def test_non_advancing_cursor_is_dropped(self):
        """A cursor at or before the current offset is not handed back.

        Resuming from it would refetch the same window forever; the result is
        reported incomplete and not resumable instead.
        """
        client = _client()
        body = {"values": [{"id": 1}], "isLastPage": False, "nextPageStart": 0}
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, body)
        with patch.object(client._session, "get", return_value=response) as mock_get:
            page = client._fetch_page("/x", limit=10)

        assert mock_get.call_count == 1
        assert page.values == [{"id": 1}]
        assert page.truncated is True
        assert page.next_page_start is None

    @pytest.mark.parametrize("bad_cursor", ["25", True, 2.5])
    def test_misshaped_cursor_is_dropped(self, bad_cursor):
        """A non-integer nextPageStart is treated as no cursor."""
        client = _client()
        body = {"values": [{"id": 1}], "isLastPage": False, "nextPageStart": bad_cursor}
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, body)
        with patch.object(client._session, "get", return_value=response):
            page = client._fetch_page("/x", limit=10)

        assert page.truncated is True
        assert page.next_page_start is None

    def test_missing_is_last_page_raises(self):
        """An envelope without isLastPage is a response-shape error rather
        than a complete list."""
        client = _client()
        body = {"values": [{"id": 1}], "nextPageStart": 25}
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, body)
        with patch.object(client._session, "get", return_value=response):
            with pytest.raises(ValueError, match="boolean 'isLastPage'"):
                client._fetch_page("/x", limit=10)

    @pytest.mark.parametrize("bad_flag", ["false", "true", 0, 1, None])
    def test_non_boolean_is_last_page_raises(self, bad_flag):
        """A non-boolean isLastPage (the string "false" included) raises."""
        client = _client()
        body = {"values": [{"id": 1}], "isLastPage": bad_flag}
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, body)
        with patch.object(client._session, "get", return_value=response):
            with pytest.raises(ValueError, match="boolean 'isLastPage'"):
                client._fetch_page("/x", limit=10)


class TestFetchPageTransform:
    """The client-side window-narrowing hook."""

    def test_transform_narrows_the_window(self):
        """The transform narrows the fetched window before it is returned."""
        client = _client()

        def only_c(values):
            return [v for v in values if v["k"] == "C"]

        with patch.object(
            client._session,
            "get",
            return_value=_page_response(
                [{"k": "A"}, {"k": "B"}, {"k": "C"}], is_last_page=True
            ),
        ) as mock_get:
            page = client._fetch_page("/x", limit=25, transform=only_c)

        assert mock_get.call_count == 1
        assert page.values == [{"k": "C"}]
        assert page.is_last_page is True
        assert page.truncated is False

    def test_empty_filtered_window_still_returns_cursor(self):
        """A window the filter empties still surfaces the raw resume cursor.

        The load-safety contract: when client-side filtering yields nothing from
        this window, the server does not fetch further pages. It hands the
        upstream cursor back so the caller resumes, with exactly one request
        issued.
        """
        client = _client()

        def match_nothing(values):
            return []

        with patch.object(
            client._session,
            "get",
            return_value=_page_response(
                [{"k": "A"}, {"k": "B"}], is_last_page=False, next_page_start=2
            ),
        ) as mock_get:
            page = client._fetch_page("/x", limit=25, transform=match_nothing)

        assert mock_get.call_count == 1
        assert page.values == []
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 2


class TestFetchPageParams:
    """Extra query-param merging and response-shape validation."""

    def test_extra_params_merged_into_the_request(self):
        """Caller params are merged alongside the start/limit cursor params."""
        client = _client()
        with patch.object(
            client._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            client._fetch_page("/x", limit=25, params={"state": "OPEN"})

        assert mock_get.call_args[1]["params"] == {
            "state": "OPEN",
            "start": 0,
            "limit": 25,
        }

    def test_additive_envelope_keys_are_tolerated(self):
        """The wire adds Cloud-style keys (page/pagelen/previous/next) to the
        DC envelope.

        The reader uses only values/isLastPage/nextPageStart, so the additive
        keys are ignored.
        """
        client = _client()
        body = {
            "values": [{"id": 1}, {"id": 2}],
            "isLastPage": True,
            "size": 2,
            "start": 0,
            "limit": 25,
            "page": 1,
            "pagelen": 25,
            "previous": None,
            "next": None,
        }
        response = MagicMock()
        response.raise_for_status.return_value = None
        attach_json(response, body)
        with patch.object(client._session, "get", return_value=response):
            page = client._fetch_page("/x", limit=25)

        assert page.values == [{"id": 1}, {"id": 2}]
        assert page.is_last_page is True
        assert page.truncated is False

    def test_non_dict_response_raises_value_error(self):
        """A non-paged body (a bare list) raises an error."""
        client = _client()
        bad = MagicMock()
        bad.raise_for_status.return_value = None
        attach_json(bad, ["not", "a", "page"])
        with patch.object(client._session, "get", return_value=bad):
            with pytest.raises(ValueError, match="unexpected response shape"):
                client._fetch_page("/x", limit=25)

    def test_non_dict_value_entry_raises_value_error(self):
        """A non-dict entry in 'values' errors instead of crashing a transform."""
        client = _client()
        body = _page_response(["junk", {"id": 1}], is_last_page=True)
        with patch.object(client._session, "get", return_value=body):
            with pytest.raises(ValueError, match="unexpected response shape"):
                client._fetch_page("/x", limit=25)
