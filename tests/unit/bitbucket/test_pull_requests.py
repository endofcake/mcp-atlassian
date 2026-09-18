"""Unit tests for PullRequestsMixin (mocked against pinned-spec shapes)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.client import (
    DEFAULT_MAX_RESPONSE_BYTES,
    BitbucketResourceNotFoundError,
    BitbucketResponseTooLargeError,
)
from mcp_atlassian.bitbucket.pull_requests import (
    MAX_CHANGES_LIMIT,
    MAX_COMMENT_TEXT_CHARS,
    MAX_CONTEXT_LINES,
    MAX_PR_DESCRIPTION_CHARS,
    MAX_PR_REVIEWERS,
    MAX_PR_TITLE_CHARS,
    MAX_PRS_LIMIT,
    PullRequestsMixin,
)
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import attach_body, attach_json


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
    attach_json(response, body)
    return response


def _json_response(body):
    """Build a mock response whose JSON body is a bare object (not paged).

    Also mocks the streamed-read surface (``headers``/``iter_content``) so the
    same helper serves size-capped fetches like the PR diff.
    """
    response = MagicMock()
    response.raise_for_status.return_value = None
    attach_json(response, body)
    raw = json.dumps(body).encode()
    response.headers = {"Content-Length": str(len(raw))}
    response.iter_content.return_value = iter([raw])
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
    """list_pull_requests: params, single-window pagination, and validation."""

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
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests"
        )
        # Single window: page size sent upstream is the default limit, plus the
        # start cursor; no filter params.
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 25}

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

    def test_enum_filters_are_normalized_on_the_wire(self):
        """Case and surrounding whitespace are normalized for the enum filters."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                "P", "r", state=" merged ", direction="outgoing", order="Oldest"
            )

        params = mock_get.call_args[1]["params"]
        assert params["state"] == "MERGED"
        assert params["direction"] == "OUTGOING"
        assert params["order"] == "OLDEST"

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"state": "CLOSED"}, "state must be one of OPEN, DECLINED, MERGED, ALL"),
            ({"direction": "SIDEWAYS"}, "direction must be one of INCOMING, OUTGOING"),
            ({"order": "RANDOM"}, "order must be one of NEWEST, OLDEST"),
        ],
    )
    def test_unknown_enum_filter_raises_without_request(self, kwargs, match):
        """An unrecognised state, direction, or order raises before any request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match=match):
                fetcher.list_pull_requests("P", "r", **kwargs)
        mock_get.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_filters_are_dropped(self, blank):
        """A blank state, direction, at, or order is omitted from the query."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                "P", "r", state=blank, direction=blank, at=blank, order=blank
            )

        params = mock_get.call_args[1]["params"]
        for name in ("state", "direction", "at", "order"):
            assert name not in params

    def test_at_is_stripped_on_the_wire(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests("P", "r", at="  refs/heads/main ")

        assert mock_get.call_args[1]["params"]["at"] == "refs/heads/main"

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [{"id": 1, "title": "a"}], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_pull_requests(project_key="P", repository_slug="r")

        assert [pr.id for pr in page.pull_requests] == [1]
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
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                project_key="P", repository_slug="r", start=25, limit=25
            )

        assert mock_get.call_args[1]["params"]["start"] == 25
        assert mock_get.call_args[1]["params"]["limit"] == 25

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
        """A slug with a slash is encoded so it cannot traverse the path."""
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

    def test_filter_text_lands_in_query_when_set(self):
        """A filter_text is sent as the upstream 'filterText' query param."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                project_key="P", repository_slug="r", filter_text="login"
            )

        assert mock_get.call_args[1]["params"]["filterText"] == "login"

    @pytest.mark.parametrize("draft, expected", [(True, "true"), (False, "false")])
    def test_draft_sends_lowercase_string(self, draft, expected):
        """A draft boolean is coerced to the lowercase string the endpoint types."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                project_key="P", repository_slug="r", draft=draft
            )

        assert mock_get.call_args[1]["params"]["draft"] == expected

    def test_absent_filters_are_omitted_from_query(self):
        """filterText and draft are absent when filter_text/draft are None."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(project_key="P", repository_slug="r")

        params = mock_get.call_args[1]["params"]
        assert "filterText" not in params
        assert "draft" not in params

    @pytest.mark.parametrize("filter_text", ["", "   "])
    def test_blank_filter_text_is_omitted(self, filter_text):
        """A blank/whitespace filter_text is treated as no filter and is omitted."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                project_key="P", repository_slug="r", filter_text=filter_text
            )

        assert "filterText" not in mock_get.call_args[1]["params"]

    def test_filter_text_is_stripped_on_the_wire(self):
        """Surrounding whitespace is trimmed from the sent filterText."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_PR], is_last_page=True),
        ) as mock_get:
            fetcher.list_pull_requests(
                project_key="P", repository_slug="r", filter_text="  login  "
            )

        assert mock_get.call_args[1]["params"]["filterText"] == "login"


class TestListUserPullRequests:
    """list_user_pull_requests: params, single-window pagination, validation."""

    _REVIEWING = {
        "id": 7,
        "title": "Fix Y",
        "state": "OPEN",
        "toRef": {
            "id": "refs/heads/main",
            "displayId": "main",
            "repository": {"slug": "svc-a", "project": {"key": "PROJ"}},
        },
        "author": {"user": {"name": "bob", "displayName": "Bob"}, "role": "AUTHOR"},
        "reviewers": [
            {
                "user": {"name": "alice", "displayName": "Alice"},
                "role": "REVIEWER",
                "approved": False,
                "status": "UNAPPROVED",
            }
        ],
    }
    _AUTHORED = {
        "id": 9,
        "title": "Add Z",
        "state": "MERGED",
        "toRef": {
            "id": "refs/heads/main",
            "displayId": "main",
            "repository": {"slug": "svc-b", "project": {"key": "OTHER"}},
        },
        "author": {"user": {"name": "alice", "displayName": "Alice"}, "role": "AUTHOR"},
        "reviewers": [],
    }

    def test_returns_models_and_default_params(self):
        """No filters: the dashboard path, the window params, no user param."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [self._REVIEWING, self._AUTHORED], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.list_user_pull_requests()

        assert [pr.id for pr in page.pull_requests] == [7, 9]
        # Each entry names its repository: ids are only unique per repository.
        assert [
            (pr.to_ref.project_key, pr.to_ref.repository_slug)
            for pr in page.pull_requests
            if pr.to_ref is not None
        ] == [("PROJ", "svc-a"), ("OTHER", "svc-b")]
        assert page.pull_requests[0].reviewers[0].user.name == "alice"
        author = page.pull_requests[1].author
        assert author is not None
        assert author.user.name == "alice"
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/rest/api/1.0/dashboard/pull-requests")
        assert mock_get.call_args[1]["params"] == {"start": 0, "limit": 25}

    def test_forwards_every_filter(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([self._REVIEWING], is_last_page=True),
        ) as mock_get:
            fetcher.list_user_pull_requests(
                user="alice",
                role="REVIEWER",
                participant_status=["UNAPPROVED", "NEEDS_WORK"],
                state="OPEN",
                order="PARTICIPANT_STATUS",
                closed_since=86400,
            )

        params = mock_get.call_args[1]["params"]
        assert params["user"] == "alice"
        assert params["role"] == "REVIEWER"
        assert params["participantStatus"] == "UNAPPROVED,NEEDS_WORK"
        assert params["state"] == "OPEN"
        assert params["order"] == "PARTICIPANT_STATUS"
        assert params["closedSince"] == 86400

    def test_user_is_stripped_on_the_wire(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_user_pull_requests(user="  alice ")

        assert mock_get.call_args[1]["params"]["user"] == "alice"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_user_is_dropped(self, blank):
        """A blank user is not sent, so the server default (the caller) applies."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_user_pull_requests(user=blank)

        assert "user" not in mock_get.call_args[1]["params"]

    def test_enum_filters_are_case_normalized(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_user_pull_requests(
                role="author",
                participant_status=[" approved "],
                state="merged",
                order="closed_date",
            )

        params = mock_get.call_args[1]["params"]
        assert params["role"] == "AUTHOR"
        assert params["participantStatus"] == "APPROVED"
        assert params["state"] == "MERGED"
        assert params["order"] == "CLOSED_DATE"

    @pytest.mark.parametrize(
        "statuses",
        [[], ["", "   "], None],
        ids=["empty-list", "blank-entries", "none"],
    )
    def test_blank_or_empty_participant_status_is_dropped(self, statuses):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_user_pull_requests(participant_status=statuses)

        assert "participantStatus" not in mock_get.call_args[1]["params"]

    def test_duplicate_participant_status_sent_once(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_user_pull_requests(
                participant_status=["APPROVED", "approved", "NEEDS_WORK"]
            )

        params = mock_get.call_args[1]["params"]
        assert params["participantStatus"] == "APPROVED,NEEDS_WORK"

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"role": "OWNER"}, "role must be one of"),
            ({"participant_status": ["APPROVED", "PENDING"]}, "participant_status"),
            ({"state": "ALL"}, "state must be one of"),
            ({"order": "RANDOM"}, "order must be one of"),
        ],
        ids=["role", "participant_status", "state-all", "order"],
    )
    def test_unrecognised_enum_raises_before_request(self, kwargs, match):
        fetcher = BitbucketFetcher(config=_byo_config())
        with (
            patch.object(fetcher._session, "get") as mock_get,
            pytest.raises(ValueError, match=match),
        ):
            fetcher.list_user_pull_requests(**kwargs)
        mock_get.assert_not_called()

    @pytest.mark.parametrize("closed_since", [0, -1, True])
    def test_non_positive_closed_since_raises_before_request(self, closed_since):
        fetcher = BitbucketFetcher(config=_byo_config())
        with (
            patch.object(fetcher._session, "get") as mock_get,
            pytest.raises(ValueError, match="closed_since"),
        ):
            fetcher.list_user_pull_requests(closed_since=closed_since)
        mock_get.assert_not_called()

    def test_single_window_issues_one_request_and_surfaces_cursor(self):
        """One upstream GET; a non-last window returns the upstream cursor."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [{"id": 1, "title": "a"}], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.list_user_pull_requests()

        assert [pr.id for pr in page.pull_requests] == [1]
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 25
        assert mock_get.call_count == 1

    def test_start_and_limit_are_clamped_and_sent(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([], is_last_page=True),
        ) as mock_get:
            fetcher.list_user_pull_requests(start=25, limit=MAX_PRS_LIMIT + 50)

        params = mock_get.call_args[1]["params"]
        assert params["start"] == 25
        assert params["limit"] == MAX_PRS_LIMIT

    def test_misshaped_page_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with (
            patch.object(
                fetcher._session,
                "get",
                return_value=_json_response({"values": "not-a-list"}),
            ),
            pytest.raises(ValueError, match="unexpected response shape"),
        ):
            fetcher.list_user_pull_requests()


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

    def test_empty_object_response_raises(self):
        """A 2xx `{}` is a shape error rather than a default pull request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get", return_value=_json_response({})):
            with pytest.raises(ValueError, match="non-empty pull-request object"):
                fetcher.get_pull_request("PROJ", "my-repo", 5)

    def test_404_raises_resource_not_found(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher.get_pull_request("P", "r", 999)


_MERGE_STATUS = {
    "canMerge": False,
    "conflicted": False,
    "outcome": "CLEAN",
    "vetoes": [
        {
            "summaryMessage": "Requires approvals",
            "detailedMessage": "You need 2 approvals before this can be merged.",
        }
    ],
}


class TestGetPullRequestMergeStatus:
    """get_pull_request_merge_status: single fetch, id validation, shape, 409."""

    def test_returns_model_with_one_request(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_MERGE_STATUS)
        ) as mock_get:
            status = fetcher.get_pull_request_merge_status("PROJ", "my-repo", 5)

        assert status.can_merge is False
        assert status.conflicted is False
        assert status.outcome == "CLEAN"
        assert [v.summary for v in status.vetoes] == ["Requires approvals"]
        assert mock_get.call_count == 1
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/merge"
        )
        assert mock_get.call_args[1]["params"] is None

    def test_conflicted(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"canMerge": False, "conflicted": True, "outcome": "CONFLICTED"}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            status = fetcher.get_pull_request_merge_status("PROJ", "my-repo", 5)

        assert status.conflicted is True
        assert status.outcome == "CONFLICTED"
        assert status.vetoes == []

    def test_missing_can_merge_is_none(self):
        """The specification omits ``canMerge``; the model tolerates its absence."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"conflicted": False, "outcome": "CLEAN", "vetoes": []}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            status = fetcher.get_pull_request_merge_status("PROJ", "my-repo", 5)

        assert status.can_merge is None
        assert status.outcome == "CLEAN"

    def test_null_vetoes_is_empty(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"conflicted": False, "outcome": "CLEAN", "vetoes": None}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            status = fetcher.get_pull_request_merge_status("PROJ", "my-repo", 5)

        assert status.vetoes == []

    def test_non_list_vetoes_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"conflicted": False, "outcome": "CLEAN", "vetoes": {}}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="'vetoes' .* is dict, not a list"):
                fetcher.get_pull_request_merge_status("PROJ", "my-repo", 5)

    @pytest.mark.parametrize("bad_id", [0, -1, "abc", None])
    def test_invalid_id_raises_without_request(self, bad_id):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="positive integer"):
                fetcher.get_pull_request_merge_status("P", "r", bad_id)
        mock_get.assert_not_called()

    @pytest.mark.parametrize(("key", "slug"), [("", "r"), ("P", " "), ("..", "r")])
    def test_blank_or_dot_segment_raises_without_request(self, key, slug):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError):
                fetcher.get_pull_request_merge_status(key, slug, 5)
        mock_get.assert_not_called()

    @pytest.mark.parametrize("body", [["not", "an", "object"], {}])
    def test_misshaped_response_raises(self, body):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="non-empty mergeability object"):
                fetcher.get_pull_request_merge_status("PROJ", "my-repo", 5)

    def test_not_open_surfaces_server_message(self):
        """A 409 (pull request merged or declined) carries the instance's message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [{"message": "The pull request is not open (state: MERGED)."}]
        }
        with patch.object(
            fetcher._session, "get", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="HTTP 409.*not open"):
                fetcher.get_pull_request_merge_status("PROJ", "my-repo", 5)

    def test_404_raises_resource_not_found(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher.get_pull_request_merge_status("P", "r", 999)


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

    def test_oversized_declared_body_aborts_before_download(self):
        """A Content-Length above the byte cap raises without reading the body."""
        fetcher = BitbucketFetcher(config=_byo_config())
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Length": str(DEFAULT_MAX_RESPONSE_BYTES + 1)}
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(ValueError, match="download cap"):
                fetcher.get_pull_request_diff("P", "r", 5)

        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_oversized_chunked_body_aborts_mid_download(self):
        """A chunked body (no Content-Length) is aborted once it exceeds the cap.

        A deliberately large response: the reader must stop pulling chunks at
        the cap instead of buffering the whole payload.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        chunk = b"x" * 65536
        chunk_count = DEFAULT_MAX_RESPONSE_BYTES // len(chunk) + 2

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {}
        response.iter_content.return_value = iter([chunk] * chunk_count)
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(ValueError, match="download cap"):
                fetcher.get_pull_request_diff("P", "r", 5)

        response.close.assert_called_once()

    def test_body_at_exactly_the_cap_succeeds(self):
        """A body of exactly DEFAULT_MAX_RESPONSE_BYTES parses (cap is inclusive)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        skeleton = b'{"pad": "", "diffs": []}'
        raw = (
            b'{"pad": "'
            + b"x" * (DEFAULT_MAX_RESPONSE_BYTES - len(skeleton))
            + b'", "diffs": []}'
        )
        assert len(raw) == DEFAULT_MAX_RESPONSE_BYTES

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Length": str(len(raw))}
        response.iter_content.return_value = iter(
            [raw[i : i + 65536] for i in range(0, len(raw), 65536)]
        )
        with patch.object(fetcher._session, "get", return_value=response):
            diff = fetcher.get_pull_request_diff("P", "r", 5)

        assert diff.files == []
        assert diff.total_files == 0

    def test_chunked_body_without_content_length_parses(self):
        """A small chunked body (no Content-Length) downloads and parses."""
        fetcher = BitbucketFetcher(config=_byo_config())
        raw = json.dumps(_DIFF_BODY).encode()
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {}
        response.iter_content.return_value = iter([raw[:10], raw[10:]])
        with patch.object(fetcher._session, "get", return_value=response):
            diff = fetcher.get_pull_request_diff("P", "r", 5)

        assert diff.total_files == len(_DIFF_BODY["diffs"])

    def test_diff_fetch_streams_with_byte_cap(self):
        """The diff request opts into streaming so the byte cap can apply."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            fetcher.get_pull_request_diff("P", "r", 5)

        assert mock_get.call_args[1]["stream"] is True


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

    def test_action_filter_narrows_one_window_and_returns_cursor(self):
        """A COMMENTED filter narrows a single fetched window (the comments
        view): the server issues ONE request, and a window with no match
        returns empty with the upstream cursor so the caller resumes."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_ACT_APPROVED], is_last_page=False, next_page_start=1
            ),
        ) as mock_get:
            page = fetcher.get_activities("P", "r", 5, action="commented")

        assert mock_get.call_count == 1
        assert page.activities == []
        assert page.is_last_page is False
        assert page.truncated is True
        assert page.next_page_start == 1

    def test_action_filter_match_after_resume(self):
        """Resuming from the returned cursor surfaces the matching comment."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response([_ACT_COMMENTED], is_last_page=True),
        ) as mock_get:
            page = fetcher.get_activities("P", "r", 5, action="commented", start=1)

        assert mock_get.call_count == 1
        assert mock_get.call_args[1]["params"]["start"] == 1
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


class TestAddComment:
    """add_comment: validation matrix, request shapes, EFFECTIVE anchor, return."""

    @staticmethod
    def _post_ok(body=None):
        """A successful POST response carrying the given (or a default) comment."""
        return _json_response(
            body if body is not None else {"id": 100, "version": 0, "text": "ok"}
        )

    def test_general_comment_request_shape(self):
        """text only → a bare general comment to the comments endpoint."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment("PROJ", "my-repo", 5, "hello")

        url = mock_post.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/comments"
        )
        assert mock_post.call_args[1]["json"] == {"text": "hello"}

    def test_reply_request_shape(self):
        """parent_id → a reply that carries the parent id and no anchor."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment("P", "r", 5, "reply", parent_id=7)

        assert mock_post.call_args[1]["json"] == {
            "text": "reply",
            "parent": {"id": 7},
        }

    def test_reply_rejects_anchor_params(self):
        """A reply combined with any anchor param is rejected before any POST."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="reply"):
                fetcher.add_comment("P", "r", 5, "x", parent_id=7, file_path="a.py")
        mock_post.assert_not_called()

    def test_reply_with_blocker_severity(self):
        """A reply can also be a BLOCKER task; both fields are emitted."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment(
                "P", "r", 5, "blocking reply", parent_id=7, severity="BLOCKER"
            )

        assert mock_post.call_args[1]["json"] == {
            "text": "blocking reply",
            "severity": "BLOCKER",
            "parent": {"id": 7},
        }

    def test_blank_file_path_rejected(self):
        """A whitespace-only file_path is rejected before any POST."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="file_path must be a non-blank"):
                fetcher.add_comment("P", "r", 5, "x", file_path="   ")
        mock_post.assert_not_called()

    def test_whole_file_comment_request_shape(self):
        """file_path only → a whole-file anchor (path, no line)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment("P", "r", 5, "fc", file_path="src/a.py")

        assert mock_post.call_args[1]["json"] == {
            "text": "fc",
            "anchor": {"path": "src/a.py"},
        }

    def test_line_comment_added_defaults_file_type_to(self):
        """An ADDED line defaults to the destination side (TO)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment(
                "P", "r", 5, "lc", file_path="a.py", line=3, line_type="added"
            )

        assert mock_post.call_args[1]["json"]["anchor"] == {
            "path": "a.py",
            "line": 3,
            "lineType": "ADDED",
            "fileType": "TO",
        }

    def test_line_comment_removed_defaults_file_type_from(self):
        """A REMOVED line defaults to the source side (FROM)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment(
                "P", "r", 5, "lc", file_path="a.py", line=3, line_type="REMOVED"
            )

        assert mock_post.call_args[1]["json"]["anchor"]["fileType"] == "FROM"

    def test_line_comment_context_defaults_file_type_to(self):
        """A CONTEXT line defaults to the destination side (TO)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment(
                "P", "r", 5, "lc", file_path="a.py", line=3, line_type="CONTEXT"
            )

        assert mock_post.call_args[1]["json"]["anchor"]["fileType"] == "TO"

    def test_line_comment_explicit_file_type_respected(self):
        """An explicit file_type overrides the line_type-derived default."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment(
                "P",
                "r",
                5,
                "lc",
                file_path="a.py",
                line=3,
                line_type="ADDED",
                file_type="from",
            )

        assert mock_post.call_args[1]["json"]["anchor"]["fileType"] == "FROM"

    def test_blocker_severity_emitted_in_line_mode(self):
        """BLOCKER is valid in any mode and emitted as a top-level severity."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment(
                "P",
                "r",
                5,
                "task",
                file_path="a.py",
                line=3,
                line_type="ADDED",
                severity="blocker",
            )

        body = mock_post.call_args[1]["json"]
        assert body["severity"] == "BLOCKER"
        assert body["anchor"]["line"] == 3

    def test_no_difftype_or_hashes_emitted(self):
        """A line anchor omits diffType/fromHash/toHash (server resolves EFFECTIVE)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=self._post_ok()
        ) as mock_post:
            fetcher.add_comment(
                "P", "r", 5, "lc", file_path="a.py", line=3, line_type="ADDED"
            )

        anchor = mock_post.call_args[1]["json"]["anchor"]
        assert set(anchor) == {"path", "line", "lineType", "fileType"}

    def test_line_without_file_path_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="line requires file_path"):
                fetcher.add_comment("P", "r", 5, "x", line=3, line_type="ADDED")
        mock_post.assert_not_called()

    def test_file_type_without_line_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="only valid together with line"):
            fetcher.add_comment("P", "r", 5, "x", file_path="a.py", file_type="TO")

    def test_blank_text_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="non-blank"):
                fetcher.add_comment("P", "r", 5, "   ")
        mock_post.assert_not_called()

    def test_bad_severity_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="severity"):
            fetcher.add_comment("P", "r", 5, "x", severity="CRITICAL")

    def test_bad_line_type_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="line_type"):
            fetcher.add_comment(
                "P", "r", 5, "x", file_path="a.py", line=3, line_type="X"
            )

    def test_bad_file_type_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="file_type"):
            fetcher.add_comment(
                "P",
                "r",
                5,
                "x",
                file_path="a.py",
                line=3,
                line_type="ADDED",
                file_type="X",
            )

    def test_line_zero_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with pytest.raises(ValueError, match="positive integer"):
            fetcher.add_comment(
                "P", "r", 5, "x", file_path="a.py", line=0, line_type="ADDED"
            )

    def test_returns_created_comment_with_version(self):
        """The 201 RestComment body is parsed, exposing id and version."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "id": 101,
            "version": 0,
            "text": "hello",
            "author": {"name": "me", "displayName": "Me"},
        }
        with patch.object(fetcher._session, "post", return_value=self._post_ok(body)):
            comment = fetcher.add_comment("P", "r", 5, "hello")

        assert comment.id == 101
        assert comment.version == 0
        assert comment.text == "hello"

    def test_non_dict_response_raises(self):
        """A non-object body raises an error."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(["not", "an", "object"]),
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.add_comment("P", "r", 5, "hello")

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"id": 101},
            {"id": 101, "version": None},
            {"id": 101, "version": "0"},
            {"version": 0},
            {"id": 0, "version": 0},
            {"id": -1, "version": 0},
            {"id": True, "version": 0},
            {"id": "101", "version": 0},
        ],
        ids=[
            "empty",
            "missing-version",
            "null-version",
            "string-version",
            "missing-id",
            "zero-id",
            "negative-id",
            "bool-id",
            "string-id",
        ],
    )
    def test_incomplete_2xx_body_raises(self, body):
        """A 2xx object without a positive id and integer version is an
        unconfirmed write, reported as an error rather than comment id 0."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="was not confirmed"):
                fetcher.add_comment("P", "r", 5, "hello")

    def test_blank_project_key_rejected_before_post(self):
        """A blank path segment is rejected before any HTTP call."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="project_key"):
                fetcher.add_comment("  ", "r", 5, "hello")
        mock_post.assert_not_called()


def _http_error_with_body(status: int, body) -> MagicMock:
    """Build a mock error response that raises and whose .json() returns body."""
    response = MagicMock()
    response.status_code = status
    attach_json(response, body)
    error = HTTPError(f"{status} error")
    error.response = response
    response.raise_for_status.side_effect = error
    return response


class TestSetReviewStatus:
    """set_review_status: enum validation, slug-anchored PUT, error surfacing.

    The username→slug resolution itself is covered in test_client.py; here it is
    stubbed so the mixin's own behaviour (path, body, projection, errors) is the
    unit under test.
    """

    def test_put_path_uses_resolved_slug_and_status_body(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher, "_resolve_current_user_slug", return_value="me-slug"
        ):
            with patch.object(
                fetcher._session,
                "put",
                return_value=_json_response(
                    {"status": "APPROVED", "user": {"name": "me"}}
                ),
            ) as mock_put:
                result = fetcher.set_review_status("PROJ", "my-repo", 5, "approved")

        url = mock_put.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/"
            "participants/me-slug"
        )
        assert mock_put.call_args[1]["json"] == {"status": "APPROVED"}
        assert result["status"] == "APPROVED"
        assert result["user"]["name"] == "me"

    def test_status_is_normalized(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher, "_resolve_current_user_slug", return_value="me"):
            with patch.object(
                fetcher._session,
                "put",
                return_value=_json_response({"status": "NEEDS_WORK"}),
            ) as mock_put:
                fetcher.set_review_status("P", "r", 5, "needs_work")

        assert mock_put.call_args[1]["json"] == {"status": "NEEDS_WORK"}

    def test_bad_status_rejected_before_resolution(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with (
            patch.object(fetcher, "_resolve_current_user_slug") as mock_resolve,
            patch.object(fetcher._session, "put") as mock_put,
        ):
            with pytest.raises(ValueError, match="status must be"):
                fetcher.set_review_status("P", "r", 5, "LGTM")
        mock_resolve.assert_not_called()
        mock_put.assert_not_called()

    def test_author_cannot_set_status_surfaces_server_message(self):
        """A 409 (author self-approve) surfaces the instance's own message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [
                {"message": "The author of a pull request may not change status."}
            ]
        }
        with patch.object(fetcher, "_resolve_current_user_slug", return_value="me"):
            with patch.object(
                fetcher._session, "put", return_value=_http_error_with_body(409, body)
            ):
                with pytest.raises(ValueError, match="may not change status"):
                    fetcher.set_review_status("P", "r", 5, "APPROVED")

    def test_slug_resolution_failure_propagates_without_put(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher,
            "_resolve_current_user_slug",
            side_effect=ValueError("no X-AUSERNAME header"),
        ):
            with patch.object(fetcher._session, "put") as mock_put:
                with pytest.raises(ValueError, match="X-AUSERNAME"):
                    fetcher.set_review_status("P", "r", 5, "APPROVED")
            mock_put.assert_not_called()

    def test_non_dict_response_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher, "_resolve_current_user_slug", return_value="me"):
            with patch.object(
                fetcher._session, "put", return_value=_json_response(["nope"])
            ):
                with pytest.raises(ValueError, match="unexpected response shape"):
                    fetcher.set_review_status("P", "r", 5, "APPROVED")

    def test_non_object_user_is_omitted_from_projection(self):
        """A confirmed status with a non-object `user` is still a success; the
        user key is simply absent from the projection."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher, "_resolve_current_user_slug", return_value="me"):
            with patch.object(
                fetcher._session,
                "put",
                return_value=_json_response({"status": "APPROVED", "user": "me"}),
            ):
                result = fetcher.set_review_status("P", "r", 5, "APPROVED")

        assert result == {"status": "APPROVED"}

    def test_long_unconfirmed_status_is_truncated_in_error(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher, "_resolve_current_user_slug", return_value="me"):
            with patch.object(
                fetcher._session,
                "put",
                return_value=_json_response({"status": "x" * 500}),
            ):
                with pytest.raises(ValueError) as excinfo:
                    fetcher.set_review_status("P", "r", 5, "APPROVED")

        assert "..." in str(excinfo.value)
        assert len(str(excinfo.value)) < 300

    @pytest.mark.parametrize(
        "body",
        [{}, {"status": None}, {"status": "NEEDS_WORK"}, {"status": "approved"}],
        ids=["empty", "null-status", "wrong-status", "wrong-case"],
    )
    def test_unconfirmed_status_raises(self, body):
        """A 2xx participant body whose status differs from the requested one
        is an unconfirmed write and raises."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher, "_resolve_current_user_slug", return_value="me"):
            with patch.object(
                fetcher._session, "put", return_value=_json_response(body)
            ):
                with pytest.raises(ValueError, match="was not confirmed"):
                    fetcher.set_review_status("P", "r", 5, "APPROVED")


def _no_content_response() -> MagicMock:
    """A 204 success: empty body, .json() would raise if ever parsed."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.side_effect = json.JSONDecodeError("no body", "", 0)
    return attach_body(response, b"")


class TestUpdateComment:
    """update_comment: validation, the minimal PUT body, and the parsed return."""

    @staticmethod
    def _put_ok(body=None):
        """A successful PUT response carrying the updated comment."""
        return _json_response(
            body if body is not None else {"id": 9, "version": 4, "text": "edited"}
        )

    def test_edit_text_sends_version_and_text(self):
        """Editing text sends {version, text} to the comment path."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "put", return_value=self._put_ok()
        ) as mock_put:
            fetcher.update_comment("PROJ", "my-repo", 5, 9, version=3, text="edited")

        url = mock_put.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/comments/9"
        )
        assert mock_put.call_args[1]["json"] == {"version": 3, "text": "edited"}

    def test_resolve_sends_version_and_thread_resolved(self):
        """Resolving sends {version, threadResolved: True}."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "threadResolved": True}
        with patch.object(
            fetcher._session, "put", return_value=self._put_ok(body)
        ) as mock_put:
            fetcher.update_comment("P", "r", 5, 9, version=3, thread_resolved=True)

        assert mock_put.call_args[1]["json"] == {
            "version": 3,
            "threadResolved": True,
        }

    def test_unresolve_sends_thread_resolved_false(self):
        """Reopening sends threadResolved: False (not dropped as falsy)."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "threadResolved": False}
        with patch.object(
            fetcher._session, "put", return_value=self._put_ok(body)
        ) as mock_put:
            fetcher.update_comment("P", "r", 5, 9, version=3, thread_resolved=False)

        assert mock_put.call_args[1]["json"] == {
            "version": 3,
            "threadResolved": False,
        }

    def test_text_and_resolve_in_one_call(self):
        """Both fields may be sent together in a single PUT."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "text": "edited", "threadResolved": True}
        with patch.object(
            fetcher._session, "put", return_value=self._put_ok(body)
        ) as mock_put:
            fetcher.update_comment(
                "P", "r", 5, 9, version=3, text="edited", thread_resolved=True
            )

        assert mock_put.call_args[1]["json"] == {
            "version": 3,
            "text": "edited",
            "threadResolved": True,
        }

    def test_returns_updated_comment_with_bumped_version(self):
        """The 200 body is parsed: bumped version and thread_resolved surface."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "text": "edited", "threadResolved": True}
        with patch.object(fetcher._session, "put", return_value=self._put_ok(body)):
            comment = fetcher.update_comment("P", "r", 5, 9, version=3, text="edited")

        assert comment.id == 9
        assert comment.version == 4
        assert comment.thread_resolved is True

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"id": 9},
            {"version": 4},
            {"id": 0, "version": 4},
            {"id": 9, "version": "4"},
            {"id": True, "version": 4},
        ],
        ids=[
            "empty",
            "missing-version",
            "missing-id",
            "zero-id",
            "string-version",
            "bool-id",
        ],
    )
    def test_incomplete_2xx_body_raises(self, body):
        """A 2xx object without a positive id and integer version is an
        unconfirmed write and raises."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="was not confirmed"):
                fetcher.update_comment("P", "r", 5, 9, version=3, text="edited")

    @pytest.mark.parametrize(
        ("body", "kwargs", "match"),
        [
            (
                {"id": 99, "version": 4, "text": "new"},
                {"text": "new"},
                "expected 'id' 9 but got 99",
            ),
            (
                {"id": 9, "version": 2, "text": "new"},
                {"text": "new"},
                "expected a 'version' of at least 3 but got 2",
            ),
            (
                {"id": 9, "version": 4, "text": "old"},
                {"text": "new"},
                "expected 'text' to echo the sent text but got 'old'",
            ),
            (
                {"id": 9, "version": 4},
                {"text": "new"},
                "expected 'text' to echo the sent text but got None",
            ),
            (
                {"id": 9, "version": 4},
                {"thread_resolved": True},
                "expected 'threadResolved' True but got None",
            ),
            (
                {"id": 9, "version": 4, "threadResolved": False},
                {"thread_resolved": True},
                "expected 'threadResolved' True but got False",
            ),
            (
                {"id": 9, "version": 4, "threadResolved": "true"},
                {"thread_resolved": True},
                "expected 'threadResolved' True but got 'true'",
            ),
            (
                {"id": 9, "version": 4, "text": "new", "threadResolved": True},
                {"text": "new", "thread_resolved": False},
                "expected 'threadResolved' False but got True",
            ),
        ],
        ids=[
            "other-id",
            "unbumped-version",
            "other-text",
            "missing-text",
            "missing-thread-state",
            "contradicting-thread-state",
            "string-thread-state",
            "text-ok-thread-state-wrong",
        ],
    )
    def test_body_disagreeing_with_request_raises(self, body, kwargs, match):
        """A 2xx body that does not confirm the sent fields is an unconfirmed
        write and raises, naming the disagreeing field."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put", return_value=_json_response(body)):
            with pytest.raises(ValueError, match=match) as excinfo:
                fetcher.update_comment("P", "r", 5, 9, version=3, **kwargs)
        assert "was not confirmed" in str(excinfo.value)

    def test_no_op_update_with_unbumped_version_is_confirmed(self):
        """Resolving an already-resolved thread echoes the sent version."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 3, "threadResolved": True}
        with patch.object(fetcher._session, "put", return_value=_json_response(body)):
            comment = fetcher.update_comment(
                "P", "r", 5, 9, version=3, thread_resolved=True
            )
        assert comment.version == 3
        assert comment.thread_resolved is True

    def test_long_unconfirmed_text_is_truncated_in_error(self):
        """The echoed upstream text in the error is bounded."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "text": "x" * 500}
        with patch.object(fetcher._session, "put", return_value=_json_response(body)):
            with pytest.raises(ValueError, match=r"\.\.\.") as excinfo:
                fetcher.update_comment("P", "r", 5, 9, version=3, text="new")
        assert len(str(excinfo.value)) < 300

    def test_numeric_string_comment_id_is_confirmed_as_int(self):
        """A "9" comment id is confirmed against the integer 9 in the body."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "text": "new"}
        with patch.object(fetcher._session, "put", return_value=_json_response(body)):
            comment = fetcher.update_comment("P", "r", 5, "9", version=3, text="new")
        assert comment.id == 9

    def test_no_field_provided_rejected_before_put(self):
        """Neither text nor thread_resolved → ValueError, no request issued."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="at least one of"):
                fetcher.update_comment("P", "r", 5, 9, version=3)
        mock_put.assert_not_called()

    @pytest.mark.parametrize("bad_version", ["3", 3.0, None, True])
    def test_non_int_version_rejected_before_put(self, bad_version):
        """A non-int version (incl. bool) is rejected before any request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="version must be an integer"):
                fetcher.update_comment("P", "r", 5, 9, version=bad_version, text="x")
        mock_put.assert_not_called()

    def test_blank_text_rejected_before_put(self):
        """A whitespace-only text is rejected before any request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="non-blank"):
                fetcher.update_comment("P", "r", 5, 9, version=3, text="   ")
        mock_put.assert_not_called()

    def test_non_numeric_comment_id_rejected_before_put(self):
        """A non-numeric comment id (e.g. a traversal attempt) raises, no request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="comment_id must be a positive"):
                fetcher.update_comment("P", "r", 5, "9/../x", version=3, text="e")
        mock_put.assert_not_called()

    @pytest.mark.parametrize("bad_id", [0, -1, "0", "-1"])
    def test_non_positive_comment_id_rejected_before_put(self, bad_id):
        """A non-positive comment id is rejected before any request is issued."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="comment_id must be a positive"):
                fetcher.update_comment("P", "r", 5, bad_id, version=3, text="e")
        mock_put.assert_not_called()

    def test_stale_version_409_surfaces_server_message(self):
        """A 409 stale-version surfaces the instance's own errors[].message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "The comment version is out of date."}]}
        with patch.object(
            fetcher._session, "put", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="out of date"):
                fetcher.update_comment("P", "r", 5, 9, version=1, text="e")

    def test_non_dict_response_raises(self):
        """A non-object body raises an error."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "put", return_value=_json_response(["nope"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.update_comment("P", "r", 5, 9, version=3, text="e")


class TestSetTaskState:
    """set_task_state: validation, the {version, state} body, confirmation, 409."""

    @staticmethod
    def _put_ok(body=None):
        """A successful PUT response carrying the updated task comment."""
        return _json_response(
            body
            if body is not None
            else {"id": 9, "version": 4, "severity": "BLOCKER", "state": "RESOLVED"}
        )

    def test_resolve_sends_only_version_and_state(self):
        """Resolving sends exactly {version, state: RESOLVED} to the comment path."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "put", return_value=self._put_ok()
        ) as mock_put:
            result = fetcher.set_task_state(
                "PROJ", "my-repo", 5, 9, version=3, state="RESOLVED"
            )

        url = mock_put.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/comments/9"
        )
        assert mock_put.call_args[1]["json"] == {"version": 3, "state": "RESOLVED"}
        assert result.id == 9
        assert result.version == 4
        assert result.state == "RESOLVED"
        assert result.severity == "BLOCKER"

    def test_reopen_sends_state_open(self):
        """Reopening sends state: OPEN and is confirmed against the response."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "severity": "BLOCKER", "state": "OPEN"}
        with patch.object(
            fetcher._session, "put", return_value=self._put_ok(body)
        ) as mock_put:
            result = fetcher.set_task_state("P", "r", 5, 9, version=3, state="OPEN")

        assert mock_put.call_args[1]["json"] == {"version": 3, "state": "OPEN"}
        assert result.state == "OPEN"

    def test_state_is_normalized_before_sending(self):
        """A lower-case or padded state is sent in the case the endpoint expects."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "put", return_value=self._put_ok()
        ) as mock_put:
            fetcher.set_task_state("P", "r", 5, 9, version=3, state=" resolved ")

        assert mock_put.call_args[1]["json"]["state"] == "RESOLVED"

    @pytest.mark.parametrize("state", ["", "DONE", "CLOSED", "threadResolved", 1])
    def test_rejects_unknown_state_before_put(self, state):
        """Anything other than RESOLVED or OPEN is rejected without a request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="state must be 'RESOLVED' or 'OPEN'"):
                fetcher.set_task_state("P", "r", 5, 9, version=3, state=state)
        mock_put.assert_not_called()

    @pytest.mark.parametrize("version", ["3", 3.0, None, True])
    def test_rejects_non_int_version_before_put(self, version):
        """The version must be a real int; nothing is sent otherwise."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="version must be an integer"):
                fetcher.set_task_state(
                    "P", "r", 5, 9, version=version, state="RESOLVED"
                )
        mock_put.assert_not_called()

    @pytest.mark.parametrize("bad_id", [0, -1, "abc", None])
    def test_rejects_bad_comment_id_before_put(self, bad_id):
        """A non-positive or non-numeric comment id is rejected without a request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="comment_id must be a positive"):
                fetcher.set_task_state("P", "r", 5, bad_id, version=3, state="RESOLVED")
        mock_put.assert_not_called()

    def test_stale_version_409_surfaces_server_message(self):
        """A 409 stale-version surfaces the instance's own errors[].message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "The comment version is out of date."}]}
        with patch.object(
            fetcher._session, "put", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="HTTP 409.*out of date"):
                fetcher.set_task_state("P", "r", 5, 9, version=1, state="RESOLVED")

    def test_missing_state_in_response_is_unconfirmed(self):
        """A 200 without a state field is an unconfirmed write."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 3, "severity": "NORMAL", "text": "not a task"}
        with patch.object(fetcher._session, "put", return_value=self._put_ok(body)):
            with pytest.raises(ValueError, match="'state' 'RESOLVED' but got None"):
                fetcher.set_task_state("P", "r", 5, 9, version=3, state="RESOLVED")

    def test_wrong_state_in_response_is_unconfirmed(self):
        """A 200 echoing a different state is an unconfirmed write."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 4, "severity": "BLOCKER", "state": "OPEN"}
        with patch.object(fetcher._session, "put", return_value=self._put_ok(body)):
            with pytest.raises(ValueError, match="'state' 'RESOLVED' but got 'OPEN'"):
                fetcher.set_task_state("P", "r", 5, 9, version=3, state="RESOLVED")

    def test_no_op_with_unbumped_version_is_confirmed(self):
        """Resolving an already-resolved task returns the sent version unchanged."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 9, "version": 3, "severity": "BLOCKER", "state": "RESOLVED"}
        with patch.object(fetcher._session, "put", return_value=self._put_ok(body)):
            result = fetcher.set_task_state("P", "r", 5, 9, version=3, state="RESOLVED")
        assert result.version == 3

    def test_wrong_id_in_response_is_unconfirmed(self):
        """A 200 carrying another comment's id is reported as unconfirmed."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"id": 10, "version": 4, "state": "RESOLVED"}
        with patch.object(fetcher._session, "put", return_value=self._put_ok(body)):
            with pytest.raises(ValueError, match="'id' 9 but got 10"):
                fetcher.set_task_state("P", "r", 5, 9, version=3, state="RESOLVED")

    def test_non_dict_response_raises(self):
        """A non-object body raises an error."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "put", return_value=_json_response(["nope"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.set_task_state("P", "r", 5, 9, version=3, state="RESOLVED")


class TestDeleteComment:
    """delete_comment: the version query param, the 204 path, error surfacing."""

    def test_sends_version_as_query_param(self):
        """The version is sent as a query param to the comment path; returns None."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_no_content_response()
        ) as mock_delete:
            result = fetcher.delete_comment("PROJ", "my-repo", 5, 9, version=3)

        assert result is None
        url = mock_delete.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/comments/9"
        )
        assert mock_delete.call_args[1]["params"] == {"version": 3}

    @pytest.mark.parametrize(
        "body", [{"errors": [{"message": "nope"}]}, {"id": 9}, ["x"]]
    )
    def test_2xx_with_body_is_unconfirmed(self, body):
        """A 2xx that carries a non-empty body is an unconfirmed delete."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_json_response(body)
        ):
            with pytest.raises(ValueError, match="was not confirmed"):
                fetcher.delete_comment("PROJ", "my-repo", 5, 9, version=3)

    @pytest.mark.parametrize("body", [{}, []])
    def test_2xx_with_empty_json_body_is_accepted(self, body):
        """An empty JSON object or list is treated like an empty body."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_json_response(body)
        ):
            assert fetcher.delete_comment("PROJ", "my-repo", 5, 9, version=3) is None

    @pytest.mark.parametrize("bad_version", ["3", 3.0, None, True])
    def test_non_int_version_rejected_before_delete(self, bad_version):
        """A non-int version (incl. bool) is rejected before any request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "delete") as mock_delete:
            with pytest.raises(ValueError, match="version must be an integer"):
                fetcher.delete_comment("P", "r", 5, 9, version=bad_version)
        mock_delete.assert_not_called()

    def test_non_numeric_comment_id_rejected_before_delete(self):
        """A non-numeric comment id (e.g. a traversal attempt) raises, no request."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "delete") as mock_delete:
            with pytest.raises(ValueError, match="comment_id must be a positive"):
                fetcher.delete_comment("P", "r", 5, "9/../x", version=3)
        mock_delete.assert_not_called()

    @pytest.mark.parametrize("bad_id", [0, -1, "0", "-1"])
    def test_non_positive_comment_id_rejected_before_delete(self, bad_id):
        """A non-positive comment id is rejected before any request is issued."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "delete") as mock_delete:
            with pytest.raises(ValueError, match="comment_id must be a positive"):
                fetcher.delete_comment("P", "r", 5, bad_id, version=3)
        mock_delete.assert_not_called()

    def test_has_replies_409_surfaces_server_message(self):
        """A 409 (has-replies/stale) surfaces the instance's own message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [{"message": "The comment has replies and cannot be deleted."}]
        }
        with patch.object(
            fetcher._session, "delete", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="has replies"):
                fetcher.delete_comment("P", "r", 5, 9, version=1)

    def test_404_raises_resource_not_found(self):
        """A 404 surfaces as the typed not-found error."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "delete", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher.delete_comment("P", "r", 999, 9, version=1)


class TestCommentTextBound:
    """Comment text is bounded client-side before a write is issued."""

    def test_add_comment_rejects_oversized_text_before_post(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        text = "x" * (MAX_COMMENT_TEXT_CHARS + 1)
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="at most 32768"):
                fetcher.add_comment("P", "r", 5, text)
        mock_post.assert_not_called()

    def test_update_comment_rejects_oversized_text_before_put(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        text = "x" * (MAX_COMMENT_TEXT_CHARS + 1)
        with patch.object(fetcher._session, "put") as mock_put:
            with pytest.raises(ValueError, match="at most 32768"):
                fetcher.update_comment("P", "r", 5, 9, version=1, text=text)
        mock_put.assert_not_called()

    def test_text_at_the_limit_is_sent(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        text = "x" * MAX_COMMENT_TEXT_CHARS
        ok = _json_response({"id": 100, "version": 0, "text": "ok"})
        with patch.object(fetcher._session, "post", return_value=ok) as mock_post:
            fetcher.add_comment("P", "r", 5, text)

        assert mock_post.call_args[1]["json"]["text"] == text


_SINGLE_FILE_DIFF = {
    "source": {"components": ["src", "old.py"], "name": "old.py"},
    "destination": {"components": ["src", "app.py"], "name": "app.py"},
    "hunks": [
        {
            "segments": [
                {"type": "ADDED", "lines": [{"line": "a"}, {"line": "b"}]},
            ]
        }
    ],
}


class TestGetPullRequestDiffNarrowing:
    """Server-side narrowing of the diff: context lines and a single file."""

    def test_context_lines_sent_server_side(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            fetcher.get_pull_request_diff("P", "r", 5, context_lines=3)

        assert mock_get.call_args[1]["params"]["contextLines"] == 3

    def test_context_lines_clamped(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            fetcher.get_pull_request_diff("P", "r", 5, context_lines=-4)
            fetcher.get_pull_request_diff("P", "r", 5, context_lines=10**9)

        sent = [c.kwargs["params"]["contextLines"] for c in mock_get.call_args_list]
        assert sent == [0, MAX_CONTEXT_LINES]

    def test_context_lines_omitted_by_default(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            fetcher.get_pull_request_diff("P", "r", 5)

        assert "contextLines" not in mock_get.call_args[1]["params"]

    def test_path_selects_single_file_form(self):
        """A path lands in the URL and the bare RestDiff is wrapped as one file."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ) as mock_get:
            diff = fetcher.get_pull_request_diff("P", "r", 5, path="src/app.py")

        assert mock_get.call_args[0][0].endswith(
            "/projects/P/repos/r/pull-requests/5/diff/src/app.py"
        )
        assert diff.total_files == 1
        assert diff.files[0].destination_path == "src/app.py"
        assert diff.files[0].source_path == "src/old.py"
        assert diff.truncated is False

    def test_path_components_are_encoded(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ) as mock_get:
            fetcher.get_pull_request_diff("P", "r", 5, path="dir name/a#b.py")

        assert mock_get.call_args[0][0].endswith("/diff/dir%20name/a%23b.py")

    @pytest.mark.parametrize("bad", ["../etc/passwd", "src//app.py", "./a.py"])
    def test_path_traversal_rejected_before_request(self, bad):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="traversal"):
                fetcher.get_pull_request_diff("P", "r", 5, path=bad)
        mock_get.assert_not_called()

    def test_src_path_sent_as_query(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ) as mock_get:
            fetcher.get_pull_request_diff(
                "P", "r", 5, path="src/app.py", src_path="src/old.py"
            )

        assert mock_get.call_args[1]["params"]["srcPath"] == "src/old.py"

    def test_src_path_is_stripped_on_the_wire(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ) as mock_get:
            fetcher.get_pull_request_diff(
                "P", "r", 5, path="src/app.py", src_path="  src/old.py "
            )

        assert mock_get.call_args[1]["params"]["srcPath"] == "src/old.py"

    def test_src_path_is_sent_unencoded_for_the_http_layer_to_encode(self):
        """A validated src_path is sent raw so it is percent-encoded once."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ) as mock_get:
            fetcher.get_pull_request_diff(
                "P", "r", 5, path="a.py", src_path="dir/my file+v1.txt"
            )

        assert mock_get.call_args[1]["params"]["srcPath"] == "dir/my file+v1.txt"

    def test_src_path_without_path_rejected_before_request(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="src_path requires path"):
                fetcher.get_pull_request_diff("P", "r", 5, src_path="src/old.py")
        mock_get.assert_not_called()

    def test_src_path_traversal_rejected(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="src_path"):
                fetcher.get_pull_request_diff(
                    "P", "r", 5, path="a.py", src_path="../b.py"
                )
        mock_get.assert_not_called()

    def test_single_file_line_cap_marks_truncated(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ):
            diff = fetcher.get_pull_request_diff(
                "P", "r", 5, path="src/app.py", max_lines_per_file=1
            )

        assert diff.truncated is True
        assert diff.files[0].omitted_lines == 1

    def test_single_file_form_accepts_the_diffs_envelope(self):
        """A wrapped body on the path form is unwrapped rather than misread."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"diffs": [_SINGLE_FILE_DIFF]}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            diff = fetcher.get_pull_request_diff("P", "r", 5, path="src/app.py")

        assert diff.total_files == 1
        assert diff.files[0].destination_path == "src/app.py"

    @pytest.mark.parametrize("body", [{}, {"size": 0}, {"lines": []}])
    def test_single_file_form_rejects_unrecognised_body(self, body):
        """A body that is neither a RestDiff nor a diffs envelope raises."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="unexpected diff response shape"):
                fetcher.get_pull_request_diff("P", "r", 5, path="src/app.py")

    def test_single_file_binary_is_surfaced(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"destination": {"components": ["img.png"]}, "binary": True}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            diff = fetcher.get_pull_request_diff("P", "r", 5, path="img.png")

        assert diff.files[0].binary is True
        assert diff.files[0].hunks == []
        assert diff.truncated is False

    def test_single_file_not_found_names_the_path(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.get_pull_request_diff("P", "r", 5, path="gone.py")

        message = str(excinfo.value)
        assert "'gone.py'" in message
        assert "src_path" in message

    @staticmethod
    def _oversized_response():
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Length": str(DEFAULT_MAX_RESPONSE_BYTES + 1)}
        return response

    def test_oversize_error_names_the_exposed_recovery_actions(self):
        """The whole-PR form keeps the typed error and names all three options."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=self._oversized_response()
        ):
            with pytest.raises(BitbucketResponseTooLargeError) as excinfo:
                fetcher.get_pull_request_diff("P", "r", 5)

        message = str(excinfo.value)
        assert "download cap" in message
        assert "bitbucket_get_pull_request_changes" in message
        assert "bitbucket_get_pull_request_diff" in message
        assert "context_lines" in message

    def test_single_file_oversize_error_names_only_context_lines(self):
        """With a path already given, lowering context_lines is the one option."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=self._oversized_response()
        ):
            with pytest.raises(BitbucketResponseTooLargeError) as excinfo:
                fetcher.get_pull_request_diff("P", "r", 5, path="a.py")

        message = str(excinfo.value)
        assert "context_lines" in message
        assert "bitbucket_get_pull_request_changes" not in message
        assert "and its path" not in message


def _change(path, change_type="MODIFY", **extra):
    """Build one RestChange entry of the changes page."""
    entry = {
        "path": {"components": path.split("/"), "name": path.rsplit("/", 1)[-1]},
        "type": change_type,
        "nodeType": "FILE",
        "contentId": "f1ce" + "0" * 36,
    }
    entry.update(extra)
    return entry


class TestGetPullRequestChanges:
    """get_pull_request_changes: one bounded window of the changes listing."""

    def test_returns_changes_and_hits_endpoint(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        values = [
            _change("src/app.py"),
            _change(
                "src/new.py",
                "MOVE",
                srcPath={"components": ["src", "old.py"], "name": "old.py"},
                percentUnchanged=98,
            ),
        ]
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(values, is_last_page=True),
        ) as mock_get:
            page = fetcher.get_pull_request_changes("PROJ", "my-repo", 5)

        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0].endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/changes"
        )
        params = mock_get.call_args[1]["params"]
        assert params["withComments"] == "false"
        assert params["start"] == 0
        assert params["limit"] == 25
        assert [c.path for c in page.changes] == ["src/app.py", "src/new.py"]
        assert page.changes[1].src_path == "src/old.py"
        assert page.changes[1].type == "MOVE"
        assert page.changes[1].percent_unchanged == 98
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

    def test_cursor_surfaced(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_change("a.py")], is_last_page=False, next_page_start=25
            ),
        ) as mock_get:
            page = fetcher.get_pull_request_changes("P", "r", 5, start=0, limit=1)

        assert mock_get.call_count == 1
        assert page.truncated is True
        assert page.next_page_start == 25

    def test_limit_clamped_to_ceiling(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.get_pull_request_changes("P", "r", 5, limit=MAX_CHANGES_LIMIT + 1)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_CHANGES_LIMIT

    def test_misshaped_page_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response({"size": 0})
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_pull_request_changes("P", "r", 5)

    def test_not_found_maps_to_resource_error(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher.get_pull_request_changes("P", "r", 5)

    def test_non_numeric_id_rejected_before_request(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError):
                fetcher.get_pull_request_changes("P", "r", "../5")
        mock_get.assert_not_called()


class TestPullRequestPathDotSegments:
    """A dot-segment project key or slug raises before any request is issued."""

    @pytest.mark.parametrize(
        "key,slug,match",
        [("..", "repo", "project_key must"), ("PROJ", "..", "repository_slug must")],
    )
    def test_dot_segments_never_reach_the_session(self, key, slug, match):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match=match):
                fetcher.list_pull_requests(key, slug)
        mock_get.assert_not_called()


def _created_pr(**overrides) -> dict:
    """A 201 body for a same-repository pull request, with optional overrides."""
    body = {
        "id": 42,
        "version": 0,
        "title": "Add feature",
        "state": "OPEN",
        "open": True,
        "closed": False,
        "fromRef": {
            "id": "refs/heads/feature/x",
            "displayId": "feature/x",
            "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
        },
        "toRef": {
            "id": "refs/heads/main",
            "displayId": "main",
            "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
        },
        "author": {"user": {"name": "me"}, "role": "AUTHOR"},
        "reviewers": [],
    }
    body.update(overrides)
    return body


class TestBuildPullRequestBody:
    """_build_pull_request_body: partial emission, validation, ref shapes."""

    build = staticmethod(PullRequestsMixin._build_pull_request_body)

    def test_only_supplied_fields_are_emitted(self):
        """A call with a subset of fields emits exactly those fields."""
        assert self.build(title="  t  ") == {"title": "t"}
        assert self.build(description="d", draft=False) == {
            "description": "d",
            "draft": False,
        }
        assert self.build() == {}

    def test_full_create_shape(self):
        """A create call carries every field in the RestPullRequest shape."""
        body = self.build(
            title="Add feature",
            description="body",
            draft=True,
            from_ref="feature/x",
            to_ref="main",
            reviewers=["alice", " bob "],
            target_repo=("PROJ", "my-repo"),
        )
        assert body == {
            "title": "Add feature",
            "description": "body",
            "draft": True,
            "fromRef": {
                "id": "refs/heads/feature/x",
                "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
            },
            "toRef": {
                "id": "refs/heads/main",
                "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
            },
            "reviewers": [{"user": {"name": "alice"}}, {"user": {"name": "bob"}}],
        }

    def test_from_repo_names_the_fork_on_the_source_only(self):
        """A fork source carries its own repository; the target keeps its own."""
        body = self.build(
            from_ref="x",
            to_ref="main",
            target_repo=("PROJ", "my-repo"),
            from_repo=("FORK", "their-repo"),
        )
        assert body["fromRef"]["repository"] == {
            "slug": "their-repo",
            "project": {"key": "FORK"},
        }
        assert body["toRef"]["repository"] == {
            "slug": "my-repo",
            "project": {"key": "PROJ"},
        }

    def test_ref_without_repository_carries_only_the_id(self):
        """Without a target repository a ref is a bare ``{id}`` object."""
        assert self.build(to_ref="main") == {"toRef": {"id": "refs/heads/main"}}

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("main", "refs/heads/main"),
            ("  feature/x  ", "refs/heads/feature/x"),
            ("refs/heads/main", "refs/heads/main"),
            ("refs/tags/v1.2", "refs/tags/v1.2"),
            ("refs/other/thing", "refs/other/thing"),
        ],
    )
    def test_source_ref_normalisation(self, given, expected):
        """A bare name is qualified; a refs/ value passes through, tags included."""
        assert self.build(from_ref=given)["fromRef"]["id"] == expected

    def test_target_tag_is_rejected(self):
        """A tag target is rejected before any request."""
        with pytest.raises(ValueError, match="to_ref must be a branch"):
            self.build(to_ref="refs/tags/v1")

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_ref_is_rejected(self, value):
        """A supplied but blank ref is rejected (None means not supplied)."""
        with pytest.raises(ValueError, match="from_ref must be a non-empty"):
            self.build(from_ref=value)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_title_is_rejected(self, value):
        with pytest.raises(ValueError, match="title must be a non-blank"):
            self.build(title=value)

    def test_oversized_title_is_rejected(self):
        with pytest.raises(ValueError, match=f"at most {MAX_PR_TITLE_CHARS}"):
            self.build(title="x" * (MAX_PR_TITLE_CHARS + 1))

    def test_title_at_the_limit_is_accepted(self):
        assert len(self.build(title="x" * MAX_PR_TITLE_CHARS)["title"]) == (
            MAX_PR_TITLE_CHARS
        )

    def test_oversized_description_is_rejected(self):
        with pytest.raises(ValueError, match=f"at most {MAX_PR_DESCRIPTION_CHARS}"):
            self.build(description="x" * (MAX_PR_DESCRIPTION_CHARS + 1))

    def test_empty_reviewers_list_is_sent(self):
        """``[]`` is sent as an explicit empty list, not dropped."""
        assert self.build(reviewers=[]) == {"reviewers": []}

    @pytest.mark.parametrize("entry", ["", "  "])
    def test_blank_reviewer_is_rejected(self, entry):
        with pytest.raises(ValueError, match="reviewers must be non-blank"):
            self.build(reviewers=["alice", entry])

    def test_repeated_reviewers_are_sent_once(self):
        """Repeated names collapse to one entry, first occurrence first."""
        body = self.build(reviewers=["alice", " alice", "bob", "alice"])
        assert body["reviewers"] == [
            {"user": {"name": "alice"}},
            {"user": {"name": "bob"}},
        ]

    def test_too_many_reviewers_is_rejected(self):
        names = [f"user{i}" for i in range(MAX_PR_REVIEWERS + 1)]
        with pytest.raises(ValueError, match=f"at most {MAX_PR_REVIEWERS}"):
            self.build(reviewers=names)

    def test_reviewers_at_the_limit_are_accepted(self):
        names = [f"user{i}" for i in range(MAX_PR_REVIEWERS)]
        assert len(self.build(reviewers=names)["reviewers"]) == MAX_PR_REVIEWERS

    @pytest.mark.parametrize("title", ["first\nsecond", "first\r\nsecond"])
    def test_multi_line_title_is_rejected(self, title):
        with pytest.raises(ValueError, match="title must be a single line"):
            self.build(title=title)


class TestCreatePullRequest:
    """create_pull_request: one POST, request shape, confirmation, errors."""

    def test_request_shape_and_confirmed_return(self):
        """A create POSTs the built body once and returns the confirmed PR."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_json_response(_created_pr())
        ) as mock_post:
            pr = fetcher.create_pull_request(
                "PROJ", "my-repo", "Add feature", "feature/x", "main"
            )

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert url.endswith("/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests")
        assert mock_post.call_args[1]["json"] == {
            "title": "Add feature",
            "fromRef": {
                "id": "refs/heads/feature/x",
                "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
            },
            "toRef": {
                "id": "refs/heads/main",
                "repository": {"slug": "my-repo", "project": {"key": "PROJ"}},
            },
        }
        assert pr.id == 42
        assert pr.version == 0
        assert pr.title == "Add feature"
        assert pr.from_ref is not None and pr.from_ref.id == "refs/heads/feature/x"
        assert pr.to_ref is not None and pr.to_ref.id == "refs/heads/main"

    def test_optional_fields_and_reviewers_are_sent(self):
        """description/draft/reviewers are sent in the body in the spec's shape."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = _created_pr(
            draft=True,
            description="d",
            reviewers=[{"user": {"name": "alice"}, "role": "REVIEWER"}],
        )
        with patch.object(
            fetcher._session, "post", return_value=_json_response(body)
        ) as mock_post:
            pr = fetcher.create_pull_request(
                "PROJ",
                "my-repo",
                "Add feature",
                "feature/x",
                "main",
                description="d",
                draft=True,
                reviewers=["alice"],
            )

        sent = mock_post.call_args[1]["json"]
        assert sent["description"] == "d"
        assert sent["draft"] is True
        assert sent["reviewers"] == [{"user": {"name": "alice"}}]
        assert pr.draft is True
        assert [r.user.name for r in pr.reviewers if r.user] == ["alice"]

    def test_from_repo_is_parsed_into_the_source_repository(self):
        """``PROJECT/slug`` is split and stripped into fromRef.repository."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = _created_pr(
            fromRef={
                "id": "refs/heads/feature/x",
                "repository": {"slug": "their-repo", "project": {"key": "FORK"}},
            }
        )
        with patch.object(
            fetcher._session, "post", return_value=_json_response(body)
        ) as mock_post:
            fetcher.create_pull_request(
                "PROJ",
                "my-repo",
                "Add feature",
                "feature/x",
                "main",
                from_repo=" FORK / their-repo ",
            )

        sent = mock_post.call_args[1]["json"]
        assert sent["fromRef"]["repository"] == {
            "slug": "their-repo",
            "project": {"key": "FORK"},
        }
        assert sent["toRef"]["repository"] == {
            "slug": "my-repo",
            "project": {"key": "PROJ"},
        }

    @pytest.mark.parametrize("bad", ["FORK", "A/B/C", "../x", " /slug", "FORK/.."])
    def test_malformed_from_repo_rejected_before_post(self, bad):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="from_repo"):
                fetcher.create_pull_request("P", "r", "t", "x", "main", from_repo=bad)
        mock_post.assert_not_called()

    def test_blank_from_repo_means_same_repository(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_json_response(_created_pr())
        ) as mock_post:
            fetcher.create_pull_request(
                "PROJ", "my-repo", "Add feature", "feature/x", "main", from_repo="  "
            )
        sent = mock_post.call_args[1]["json"]
        assert sent["fromRef"]["repository"]["slug"] == "my-repo"

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"title": "   "}, "title must be a non-blank"),
            ({"from_ref": " "}, "from_ref must be a non-empty"),
            ({"to_ref": "refs/tags/v1"}, "to_ref must be a branch"),
            ({"reviewers": [" "]}, "reviewers must be non-blank"),
            ({"project_key": " "}, "project_key"),
        ],
    )
    def test_invalid_input_rejected_before_post(self, kwargs, match):
        """Client-side validation fails before any HTTP call is made."""
        fetcher = BitbucketFetcher(config=_byo_config())
        args = {
            "project_key": "P",
            "repository_slug": "r",
            "title": "t",
            "from_ref": "x",
            "to_ref": "main",
        }
        args.update(kwargs)
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match=match):
                fetcher.create_pull_request(**args)
        mock_post.assert_not_called()

    def test_400_surfaces_server_message(self):
        """A 400 (malformed entity) carries the instance's own message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "The pull request title is required."}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(400, body)
        ):
            with pytest.raises(ValueError, match="HTTP 400.*title is required"):
                fetcher.create_pull_request("P", "r", "t", "x", "main")

    def test_409_surfaces_server_message(self):
        """A 409 (duplicate, same refs, unresolved reviewer) carries the message."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [
                {
                    "message": "Only one pull request may be open for a given source "
                    "and target branch."
                }
            ]
        }
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="HTTP 409.*Only one pull request"):
                fetcher.create_pull_request("P", "r", "t", "x", "main")

    def test_404_names_both_refs(self):
        """A 404 names the qualified refs, the usual missing piece on a create."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.create_pull_request("P", "r", "t", "x", "main")
        message = str(excinfo.value)
        assert "HTTP 404" in message
        assert "'refs/heads/x'" in message
        assert "'refs/heads/main'" in message
        assert " in " not in message

    def test_404_names_the_fork_for_a_cross_repository_source(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError, match="in FORK/r2"):
                fetcher.create_pull_request(
                    "P", "r", "t", "x", "main", from_repo="FORK/r2"
                )

    def test_non_dict_response_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_json_response(["not", "a", "pr"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.create_pull_request("P", "r", "t", "x", "main")

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"id": 42},
            {"id": 42, "version": "0"},
            {"id": 0, "version": 0},
            {"id": True, "version": 0},
            {"version": 0},
        ],
        ids=[
            "empty",
            "missing-version",
            "string-version",
            "zero-id",
            "bool-id",
            "missing-id",
        ],
    )
    def test_incomplete_2xx_body_raises(self, body):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="was not confirmed"):
                fetcher.create_pull_request("P", "r", "t", "x", "main")

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"title": "Other"}, "'title'"),
            ({"fromRef": {"id": "refs/heads/else"}}, "'fromRef.id'"),
            ({"fromRef": "refs/heads/feature/x"}, "'fromRef.id'"),
            ({"toRef": {"id": "refs/heads/develop"}}, "'toRef.id'"),
            ({"toRef": {}}, "'toRef.id'"),
        ],
        ids=["title", "from-ref", "from-ref-not-object", "to-ref", "to-ref-no-id"],
    )
    def test_mismatched_2xx_body_raises(self, overrides, match):
        """A 2xx body that disagrees with the request is an unconfirmed write."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_created_pr(**overrides)),
        ):
            with pytest.raises(ValueError, match=f"{match}.*was not confirmed"):
                fetcher.create_pull_request(
                    "PROJ", "my-repo", "Add feature", "feature/x", "main"
                )

    def test_confirmation_uses_the_qualified_ref(self):
        """A bare ref is confirmed against its refs/heads/ form, not as given."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_created_pr(toRef={"id": "main"})),
        ):
            with pytest.raises(ValueError, match="'toRef.id' 'refs/heads/main'"):
                fetcher.create_pull_request(
                    "PROJ", "my-repo", "Add feature", "feature/x", "main"
                )

    @pytest.mark.parametrize(
        "body",
        [_created_pr(), _created_pr(draft=False), _created_pr(draft="true")],
        ids=["absent", "false", "string"],
    )
    def test_draft_not_echoed_raises(self, body):
        """A server that ignores ``draft`` is reported as an unconfirmed write."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="'draft' True.*was not confirmed"):
                fetcher.create_pull_request(
                    "PROJ", "my-repo", "Add feature", "feature/x", "main", draft=True
                )

    def test_draft_is_not_checked_when_not_sent(self):
        """Without a draft flag in the request, the response's value is free."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_created_pr(draft=False)),
        ):
            pr = fetcher.create_pull_request(
                "PROJ", "my-repo", "Add feature", "feature/x", "main"
            )
        assert pr.draft is False


def _lifecycle_body(state: str, *, pr_id: int = 5, version: int = 4) -> dict:
    """A RestPullRequest body as a merge, decline, or reopen returns it."""
    return {
        "id": pr_id,
        "version": version,
        "title": "Add X",
        "state": state,
        "open": state == "OPEN",
        "closed": state != "OPEN",
        "fromRef": {"id": "refs/heads/feature", "displayId": "feature"},
        "toRef": {"id": "refs/heads/main", "displayId": "main"},
    }


class TestMergePullRequest:
    """merge_pull_request: the body, the version in query and body, confirmation."""

    def test_sends_version_in_query_and_body_with_no_extras(self):
        """A minimal merge sends only the version: no autoMerge, no strategy."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_lifecycle_body("MERGED")),
        ) as mock_post:
            result = fetcher.merge_pull_request("PROJ", "my-repo", 5, version=3)

        url = mock_post.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/merge"
        )
        assert mock_post.call_args[1]["params"] == {"version": 3}
        assert mock_post.call_args[1]["json"] == {"version": 3}
        assert result.state == "MERGED"
        assert result.version == 4

    def test_message_and_strategy_are_forwarded_verbatim(self):
        """message and strategyId are sent as given, and autoMerge is not sent."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_lifecycle_body("MERGED")),
        ) as mock_post:
            fetcher.merge_pull_request(
                "P", "r", 5, version=3, message="Merge it", strategy_id="squash"
            )

        body = mock_post.call_args[1]["json"]
        assert body == {"version": 3, "message": "Merge it", "strategyId": "squash"}
        assert "autoMerge" not in body
        assert "autoSubject" not in body

    def test_unknown_strategy_is_passed_through_for_the_server_to_reject(self):
        """The strategy id is not validated client-side, because the server owns
        the list.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "Merge strategy 'octopus' is not enabled."}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(400, body)
        ) as mock_post:
            with pytest.raises(ValueError, match="not enabled"):
                fetcher.merge_pull_request(
                    "P", "r", 5, version=3, strategy_id="octopus"
                )
        assert mock_post.call_args[1]["json"]["strategyId"] == "octopus"

    @pytest.mark.parametrize("bad_version", ["3", 3.0, None, True, -1])
    def test_non_int_version_rejected_before_post(self, bad_version):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="version must be a non-negative"):
                fetcher.merge_pull_request("P", "r", 5, version=bad_version)
        mock_post.assert_not_called()

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"message": "  "}, "message must be a non-blank"),
            ({"message": "m" * (MAX_COMMENT_TEXT_CHARS + 1)}, "message is 32769"),
            ({"strategy_id": ""}, "strategy_id must be a non-blank"),
        ],
    )
    def test_blank_optionals_rejected_before_post(self, kwargs, match):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match=match):
                fetcher.merge_pull_request("P", "r", 5, version=3, **kwargs)
        mock_post.assert_not_called()

    def test_stale_version_409_surfaces_server_message(self):
        """A 409 (stale version, veto, or conflict) carries the server's reason."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [
                {
                    "message": "You are attempting to modify a pull request based on "
                    "out-of-date information."
                }
            ]
        }
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="out-of-date information"):
                fetcher.merge_pull_request("P", "r", 5, version=1)

    def test_merge_check_veto_409_surfaces_server_message(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [
                {"message": "Requires 2 approvals. Vetoed by a merge check."},
            ]
        }
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="Vetoed by a merge check"):
                fetcher.merge_pull_request("P", "r", 5, version=3)

    def test_403_surfaces_server_message_as_auth_error(self):
        """A 403 (repository setting or permission) keeps the auth type and text."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {
            "errors": [
                {"message": "You do not have write permission for this repository."}
            ]
        }
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(403, body)
        ):
            with pytest.raises(
                MCPAtlassianAuthenticationError, match="write permission"
            ):
                fetcher.merge_pull_request("P", "r", 5, version=3)

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            (["nope"], "unexpected response shape"),
            (_lifecycle_body("OPEN"), "expected 'state' 'MERGED' but got 'OPEN'"),
            (_lifecycle_body("MERGED", pr_id=6), "expected 'id' 5 but got 6"),
            (
                _lifecycle_body("MERGED", version=3),
                "expected a 'version' above 3 but got 3",
            ),
            (
                _lifecycle_body("MERGED", version="4"),
                "expected a 'version' above 3 but got '4'",
            ),
            ({"id": 5, "state": "MERGED"}, "expected a 'version' above 3"),
        ],
    )
    def test_unconfirmed_body_raises(self, body, match):
        """A 2xx that does not confirm the merge raises."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post", return_value=_json_response(body)):
            with pytest.raises(ValueError, match=match) as excinfo:
                fetcher.merge_pull_request("P", "r", 5, version=3)
        if not isinstance(body, list):
            assert "may have been applied but was not confirmed" in str(excinfo.value)

    def test_not_found_propagates(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(404, {})
        ):
            with pytest.raises(BitbucketResourceNotFoundError):
                fetcher.merge_pull_request("P", "r", 5, version=3)

    def test_non_positive_id_rejected_before_post(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="pull_request_id must be"):
                fetcher.merge_pull_request("P", "r", 0, version=3)
        mock_post.assert_not_called()


class TestDeclinePullRequest:
    """decline_pull_request: the body, the version in query and body, confirmation."""

    def test_sends_version_in_query_and_body(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_lifecycle_body("DECLINED")),
        ) as mock_post:
            result = fetcher.decline_pull_request("PROJ", "my-repo", 5, version=3)

        url = mock_post.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/decline"
        )
        assert mock_post.call_args[1]["params"] == {"version": 3}
        assert mock_post.call_args[1]["json"] == {"version": 3}
        assert result.state == "DECLINED"
        assert result.version == 4

    def test_comment_is_forwarded(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_lifecycle_body("DECLINED")),
        ) as mock_post:
            fetcher.decline_pull_request("P", "r", 5, version=3, comment="Superseded")

        assert mock_post.call_args[1]["json"] == {
            "version": 3,
            "comment": "Superseded",
        }

    @pytest.mark.parametrize(
        ("comment", "match"),
        [
            (" ", "comment must be a non-blank"),
            ("c" * (MAX_COMMENT_TEXT_CHARS + 1), "comment is 32769"),
        ],
    )
    def test_bad_comment_rejected_before_post(self, comment, match):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match=match):
                fetcher.decline_pull_request("P", "r", 5, version=3, comment=comment)
        mock_post.assert_not_called()

    def test_non_ascii_comment_is_forwarded_unchanged(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_lifecycle_body("DECLINED")),
        ) as mock_post:
            fetcher.decline_pull_request("P", "r", 5, version=3, comment="Zamknięte 🚫")

        assert mock_post.call_args[1]["json"]["comment"] == "Zamknięte 🚫"

    @pytest.mark.parametrize("bad_version", ["3", 3.0, None, False, -1])
    def test_non_int_version_rejected_before_post(self, bad_version):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="version must be a non-negative"):
                fetcher.decline_pull_request("P", "r", 5, version=bad_version)
        mock_post.assert_not_called()

    def test_stale_version_409_surfaces_server_message(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "The pull request is not open."}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="is not open"):
                fetcher.decline_pull_request("P", "r", 5, version=1)

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            (_lifecycle_body("OPEN"), "expected 'state' 'DECLINED' but got 'OPEN'"),
            (_lifecycle_body("DECLINED", version=2), "a 'version' above 3 but got 2"),
        ],
    )
    def test_unconfirmed_body_raises(self, body, match):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post", return_value=_json_response(body)):
            with pytest.raises(ValueError, match=match):
                fetcher.decline_pull_request("P", "r", 5, version=3)


class TestReopenPullRequest:
    """reopen_pull_request: the body, the version in query and body, confirmation."""

    def test_sends_version_in_query_and_body(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(_lifecycle_body("OPEN")),
        ) as mock_post:
            result = fetcher.reopen_pull_request("PROJ", "my-repo", 5, version=3)

        url = mock_post.call_args[0][0]
        assert url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/pull-requests/5/reopen"
        )
        assert mock_post.call_args[1]["params"] == {"version": 3}
        assert mock_post.call_args[1]["json"] == {"version": 3}
        assert result.state == "OPEN"
        assert result.version == 4

    @pytest.mark.parametrize("bad_version", ["3", 3.0, None, True, -1])
    def test_non_int_version_rejected_before_post(self, bad_version):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post") as mock_post:
            with pytest.raises(ValueError, match="version must be a non-negative"):
                fetcher.reopen_pull_request("P", "r", 5, version=bad_version)
        mock_post.assert_not_called()

    def test_merged_pull_request_409_surfaces_server_message(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"errors": [{"message": "A merged pull request cannot be reopened."}]}
        with patch.object(
            fetcher._session, "post", return_value=_http_error_with_body(409, body)
        ):
            with pytest.raises(ValueError, match="cannot be reopened"):
                fetcher.reopen_pull_request("P", "r", 5, version=3)

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            (
                _lifecycle_body("DECLINED"),
                "expected 'state' 'OPEN' but got 'DECLINED'",
            ),
            (_lifecycle_body("OPEN", version=3), "a 'version' above 3 but got 3"),
        ],
    )
    def test_unconfirmed_body_raises(self, body, match):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "post", return_value=_json_response(body)):
            with pytest.raises(ValueError, match=match):
                fetcher.reopen_pull_request("P", "r", 5, version=3)
