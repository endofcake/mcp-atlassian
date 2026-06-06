"""Unit tests for the shared BitbucketClient._paginate helper."""

from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.bitbucket import BitbucketConfig
from mcp_atlassian.bitbucket.client import BitbucketClient
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of a Bitbucket paged endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
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


class TestPaginateWalk:
    """Page-walking, limit, and safety-cap behaviour."""

    def test_single_last_page_is_complete(self):
        """A single last page returns its values, complete and untruncated."""
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response([{"id": 1}, {"id": 2}], is_last_page=True),
        ) as mock_get:
            page = client._paginate("/x", limit=25, page_size=100, max_pages=10)

        assert page.values == [{"id": 1}, {"id": 2}]
        assert page.is_last_page is True
        assert page.truncated is False
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 100}

    def test_walks_pages_until_last_page(self):
        """Pages are followed via nextPageStart and concatenated."""
        client = _client()
        pages = [
            _page_response([{"id": 1}], is_last_page=False, next_page_start=1),
            _page_response([{"id": 2}], is_last_page=True),
        ]
        with patch.object(client._session, "get", side_effect=pages) as mock_get:
            page = client._paginate("/x", limit=25, page_size=100, max_pages=10)

        assert page.values == [{"id": 1}, {"id": 2}]
        assert page.is_last_page is True
        assert page.truncated is False
        assert mock_get.call_args_list[0][1]["params"]["start"] == 0
        assert mock_get.call_args_list[1][1]["params"]["start"] == 1

    def test_truncates_at_limit_without_overfetching(self):
        """A small limit against a large page stops after one page, flags truncation."""
        client = _client()
        big_page = _page_response(
            [{"id": i} for i in range(100)], is_last_page=False, next_page_start=100
        )
        with patch.object(client._session, "get", return_value=big_page) as mock_get:
            page = client._paginate("/x", limit=25, page_size=100, max_pages=10)

        assert len(page.values) == 25
        assert page.truncated is True
        assert page.is_last_page is False
        assert mock_get.call_count == 1

    def test_last_page_overshooting_limit_is_truncated(self):
        """A complete final page larger than limit trims and flags truncation."""
        client = _client()
        single_page = _page_response([{"id": i} for i in range(60)], is_last_page=True)
        with patch.object(client._session, "get", return_value=single_page) as mock_get:
            page = client._paginate("/x", limit=25, page_size=100, max_pages=10)

        assert len(page.values) == 25
        assert page.is_last_page is True
        assert page.truncated is True
        assert mock_get.call_count == 1

    def test_max_pages_cap_truncates(self):
        """The page cap bounds the walk; an endless list truncates, not last page."""
        client = _client()

        def endless(url, params=None, timeout=None):
            start = params["start"]
            return _page_response(
                [{"id": start + i} for i in range(100)],
                is_last_page=False,
                next_page_start=start + 100,
            )

        with patch.object(client._session, "get", side_effect=endless) as mock_get:
            page = client._paginate("/x", limit=10_000, page_size=100, max_pages=3)

        assert mock_get.call_count == 3
        assert len(page.values) == 300
        assert page.is_last_page is False
        assert page.truncated is True

    def test_non_last_page_without_cursor_stops_and_truncates(self):
        """isLastPage false but no nextPageStart: stop and report incomplete.

        A DC instance can answer with ``isLastPage: false`` yet omit
        ``nextPageStart``. The walk must not loop or send ``start=None``; it stops
        and signals truncation so the caller does not treat the result as
        complete.
        """
        client = _client()
        with patch.object(
            client._session,
            "get",
            return_value=_page_response([{"id": 1}], is_last_page=False),
        ) as mock_get:
            page = client._paginate("/x", limit=25, page_size=100, max_pages=10)

        assert page.values == [{"id": 1}]
        assert page.is_last_page is False
        assert page.truncated is True
        assert mock_get.call_count == 1

    def test_empty_last_page_is_not_truncated(self):
        """An empty, last page yields an empty, complete result (not an error)."""
        client = _client()
        with patch.object(
            client._session, "get", return_value=_page_response([], is_last_page=True)
        ):
            page = client._paginate("/x", limit=25, page_size=100, max_pages=10)

        assert page.values == []
        assert page.is_last_page is True
        assert page.truncated is False


class TestPaginateTransform:
    """The per-page transform hook."""

    def test_transform_applied_per_page(self):
        """The transform narrows each page before the items are collected."""
        client = _client()
        pages = [
            _page_response(
                [{"k": "A"}, {"k": "B"}], is_last_page=False, next_page_start=2
            ),
            _page_response([{"k": "C"}], is_last_page=True),
        ]

        def only_c(values):
            return [v for v in values if v["k"] == "C"]

        with patch.object(client._session, "get", side_effect=pages):
            page = client._paginate(
                "/x", limit=25, page_size=100, max_pages=10, transform=only_c
            )

        assert page.values == [{"k": "C"}]
        assert page.is_last_page is True
        assert page.truncated is False


class TestPaginateParams:
    """Extra query-param merging and response-shape validation."""

    def test_extra_params_merged_into_each_request(self):
        """Caller params are merged alongside the start/limit cursor params."""
        client = _client()
        with patch.object(
            client._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            client._paginate(
                "/x",
                limit=25,
                page_size=50,
                max_pages=10,
                params={"state": "OPEN"},
            )

        assert mock_get.call_args[1]["params"] == {
            "state": "OPEN",
            "start": 0,
            "limit": 50,
        }

    def test_non_dict_response_raises_value_error(self):
        """A non-paged body (a bare list) is an error, not a silent empty result."""
        client = _client()
        bad = MagicMock()
        bad.raise_for_status.return_value = None
        bad.json.return_value = ["not", "a", "page"]
        with patch.object(client._session, "get", return_value=bad):
            with pytest.raises(ValueError, match="unexpected response shape"):
                client._paginate("/x", limit=25, page_size=100, max_pages=10)

    def test_non_dict_value_entry_raises_value_error(self):
        """A non-dict entry in 'values' errors instead of crashing a transform."""
        client = _client()
        body = _page_response(["junk", {"id": 1}], is_last_page=True)
        with patch.object(client._session, "get", return_value=body):
            with pytest.raises(ValueError, match="unexpected response shape"):
                client._paginate("/x", limit=25, page_size=100, max_pages=10)
