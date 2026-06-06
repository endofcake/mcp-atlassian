"""Unit tests for PullRequestsMixin (mocked against pinned-spec shapes)."""

from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.client import BitbucketResourceNotFoundError
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig


def _byo_config() -> BitbucketConfig:
    """Build a DC BYO-token config for client construction."""
    return BitbucketConfig(
        url="https://bitbucket.corp.example.com",
        auth_type="oauth",
        oauth_config=BYOAccessTokenOAuthConfig(
            access_token="user-bearer-token",
            base_url="https://bitbucket.corp.example.com",
        ),
    )


def _page_response(values, is_last_page, next_page_start=None):
    """Build a mock response for one page of a paged endpoint."""
    body = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def _json_response(body):
    """Build a mock response whose JSON body is a bare object (not paged)."""
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


_PR = {
    "id": 5,
    "title": "Add X",
    "state": "OPEN",
    "fromRef": {"id": "refs/heads/x", "displayId": "x"},
    "toRef": {"id": "refs/heads/main", "displayId": "main"},
    "participants": [{"user": {"name": "a"}, "role": "AUTHOR"}],
    "reviewers": [{"user": {"name": "r"}, "status": "APPROVED", "approved": True}],
}


class TestListPullRequests:
    """list_pull_requests: params, pagination, and validation."""

    def test_returns_models_and_default_params(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            page = fetcher.list_pull_requests(
                project_key="PROJ", repository_slug="my-repo"
            )

        assert len(page.pull_requests) == 1
        assert page.pull_requests[0].id == 5
        assert page.pull_requests[0].title == "Add X"
        assert page.is_last_page is True
        assert page.truncated is False
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests"
        )
        # With no filters set, only start/limit pagination params are sent.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 50}

    def test_passes_state_direction_at_order(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                project_key="PROJ",
                repository_slug="my-repo",
                state="MERGED",
                direction="OUTGOING",
                at="refs/heads/main",
                order="OLDEST",
            )

        params = mock_get.call_args[1]["params"]
        assert params["state"] == "MERGED"
        assert params["direction"] == "OUTGOING"
        assert params["at"] == "refs/heads/main"
        assert params["order"] == "OLDEST"

    def test_walks_pages_until_last(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        pages = [
            _page_response(
                [{"id": 1, "title": "a"}], is_last_page=False, next_page_start=1
            ),
            _page_response([{"id": 2, "title": "b"}], is_last_page=True),
        ]
        with patch.object(fetcher._session, "get", side_effect=pages):
            page = fetcher.list_pull_requests(project_key="P", repository_slug="r")

        assert [pr.id for pr in page.pull_requests] == [1, 2]
        assert page.is_last_page is True

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_project_key_raises(self, blank):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="project_key"):
            fetcher.list_pull_requests(project_key=blank, repository_slug="r")

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_repository_slug_raises(self, blank):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="repository_slug"):
            fetcher.list_pull_requests(project_key="P", repository_slug=blank)

    def test_path_segments_are_percent_encoded(self):
        """A slug with a slash is encoded, not left to traverse the path."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(project_key="P", repository_slug="a/b")

        called_url = mock_get.call_args[0][0]
        assert "a%2Fb" in called_url
        assert "/repos/a/b/" not in called_url


class TestGetPullRequest:
    """get_pull_request: single fetch, id validation, shape, and 404."""

    def test_returns_model(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_PR)
        ) as mock_get:
            pr = fetcher.get_pull_request("PROJ", "my-repo", 5)

        assert pr.id == 5
        # _PR has no top-level author, so this exercises the participant fallback.
        assert pr.author is not None and pr.author.role == "AUTHOR"
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5"
        )

    @pytest.mark.parametrize("bad_id", [0, -1, "abc", None])
    def test_invalid_id_raises(self, bad_id):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="positive integer"):
            fetcher.get_pull_request("P", "r", bad_id)

    def test_non_dict_response_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(["not", "a", "pr"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_pull_request("P", "r", 5)

    def test_404_raises_resource_not_found(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher.get_pull_request("P", "r", 999)


_DIFF_BODY = {
    "fromHash": "abc123",
    "toHash": "def456",
    "diffs": [
        {
            "destination": {"components": ["src", "app.py"], "name": "app.py"},
            "hunks": [
                {
                    "segments": [
                        {
                            "type": "ADDED",
                            "lines": [
                                {"destination": 1, "line": "a"},
                                {"destination": 2, "line": "b"},
                                {"destination": 3, "line": "c"},
                            ],
                        }
                    ]
                }
            ],
        }
    ],
    "truncated": False,
}


class TestGetPullRequestDiff:
    """get_pull_request_diff: single fetch, withComments off, truncation."""

    def test_returns_diff_and_forces_comments_off(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            diff = fetcher.get_pull_request_diff("PROJ", "my-repo", 5)

        assert diff.total_files == 1
        assert diff.files[0].destination_path == "src/app.py"
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/diff"
        )
        # Inline comments are kept out of the diff payload.
        assert mock_get.call_args[1]["params"]["withComments"] == "false"

    def test_applies_max_lines_per_file(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ):
            diff = fetcher.get_pull_request_diff("P", "r", 5, max_lines_per_file=2)

        file = diff.files[0]
        kept = sum(len(seg.lines) for hunk in file.hunks for seg in hunk.segments)
        assert kept == 2
        assert file.line_truncated is True
        assert file.omitted_lines == 1
        assert diff.truncated is True

    def test_non_dict_response_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(["nope"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_pull_request_diff("P", "r", 5)

    def test_max_files_caps_file_list(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"diffs": [{"destination": {"name": f"f{i}"}} for i in range(5)]}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            diff = fetcher.get_pull_request_diff("P", "r", 5, max_files=2)

        assert diff.total_files == 5
        assert len(diff.files) == 2
        assert diff.truncated is True


_ACT_COMMENTED = {
    "id": 1,
    "action": "COMMENTED",
    "user": {"name": "r"},
    "comment": {"id": 9, "text": "nit", "author": {"name": "r"}},
}
_ACT_APPROVED = {"id": 2, "action": "APPROVED", "user": {"name": "r"}}


class TestGetActivities:
    """get_activities: timeline, and the client-side comment filter."""

    def test_returns_activities(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_ACT_APPROVED, _ACT_COMMENTED], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.get_activities("PROJ", "my-repo", 5)

        assert [a.action for a in page.activities] == ["APPROVED", "COMMENTED"]
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/activities"
        )

    def test_action_filter_collects_only_matches_across_pages(self):
        """A COMMENTED filter walks past non-comment pages (the comments view)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        pages = [
            _page_response([_ACT_APPROVED], is_last_page=False, next_page_start=1),
            _page_response([_ACT_COMMENTED], is_last_page=True),
        ]
        with patch.object(fetcher._session, "get", side_effect=pages):
            page = fetcher.get_activities("P", "r", 5, action="commented")

        assert len(page.activities) == 1
        assert page.activities[0].action == "COMMENTED"
        assert page.activities[0].comment is not None
        assert page.activities[0].comment.text == "nit"

    def test_invalid_id_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="positive integer"):
            fetcher.get_activities("P", "r", 0)

    def test_commented_activity_without_payload_has_no_comment(self):
        """A COMMENTED entry with no comment object yields comment=None.

        The activities timeline can carry a COMMENTED action whose comment
        payload is absent; it still passes the filter but surfaces no comment.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        payloadless = {"id": 1, "action": "COMMENTED", "user": {"name": "r"}}
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([payloadless], is_last_page=True),
        ):
            page = fetcher.get_activities("P", "r", 5, action="COMMENTED")

        assert len(page.activities) == 1
        assert page.activities[0].action == "COMMENTED"
        assert page.activities[0].comment is None
