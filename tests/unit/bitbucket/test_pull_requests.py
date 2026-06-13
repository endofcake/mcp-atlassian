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


class TestAddComment:
    """add_comment: validation matrix, request shapes, EFFECTIVE anchor, return."""

    @staticmethod
    def _post_ok(body=None):
        """A successful POST response carrying the given (or a default) comment."""
        return _json_response(body or {"id": 100, "version": 0, "text": "ok"})

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
        """A non-object body is an error, not a silent empty comment."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "post",
            return_value=_json_response(["not", "an", "object"]),
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
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
    response.json.return_value = body
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
