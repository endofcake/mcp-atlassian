"""Unit tests for CompareMixin.

Responses are mocked against the shapes the Bitbucket Data Center 9.4 REST specification
declares.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError, Request

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.client import (
    DEFAULT_MAX_RESPONSE_BYTES,
    BitbucketResourceNotFoundError,
    BitbucketResponseTooLargeError,
)
from mcp_atlassian.bitbucket.commits import MAX_COMMITS_LIMIT
from mcp_atlassian.bitbucket.pull_requests import (
    MAX_CHANGES_LIMIT,
    MAX_CONTEXT_LINES,
)
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import attach_json


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
    same helper serves size-capped fetches like the compare diff.
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


def _oversized_response() -> MagicMock:
    """Build a mock response advertising a body above the byte cap."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"Content-Length": str(DEFAULT_MAX_RESPONSE_BYTES + 1)}
    return response


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


def _commit(display_id, *, message="msg"):
    """Build one RestCommit entry of the commits page."""
    return {
        "id": display_id + "0" * (40 - len(display_id)),
        "displayId": display_id,
        "message": message,
        "author": {"name": "dev", "emailAddress": "dev@example.com"},
        "authorTimestamp": 1700000000000,
        "parents": [{"id": "p" * 40, "displayId": "ppppppp"}],
    }


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


class TestCompareParams:
    """The query contract shared by the three compare endpoints."""

    def test_from_ref_is_required(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="from_ref"):
                fetcher.compare_changes("P", "r", "   ")
        mock_get.assert_not_called()

    def test_to_ref_omitted_is_not_sent(self):
        """An omitted target lets the server substitute the default branch."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_changes("P", "r", "feature/x")

        params = mock_get.call_args[1]["params"]
        assert params["from"] == "feature/x"
        assert "to" not in params
        assert "fromRepo" not in params

    def test_blank_to_ref_is_dropped(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_changes("P", "r", "feature/x", to_ref="  ")

        assert "to" not in mock_get.call_args[1]["params"]

    def test_refs_are_stripped_and_sent_as_query_values(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_changes("P", "r", " refs/tags/v1.2 ", to_ref=" abc123 ")

        params = mock_get.call_args[1]["params"]
        assert params["from"] == "refs/tags/v1.2"
        assert params["to"] == "abc123"

    def test_from_repo_is_sent_as_project_and_slug(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_changes("P", "r", "x", from_repo=" FORK / my-repo ")

        assert mock_get.call_args[1]["params"]["fromRepo"] == "FORK/my-repo"

    def test_blank_from_repo_is_dropped(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_changes("P", "r", "x", from_repo="  ")

        assert "fromRepo" not in mock_get.call_args[1]["params"]

    @pytest.mark.parametrize(
        "bad",
        ["42", "FORK", "FORK/my-repo/extra", "/my-repo", "FORK/", "../x", "a/.."],
    )
    def test_malformed_from_repo_rejected_before_request(self, bad):
        """Only the PROJECT/slug form is accepted; the numeric id form is not."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="from_repo"):
                fetcher.compare_changes("P", "r", "x", from_repo=bad)
        mock_get.assert_not_called()


class TestCompareChanges:
    """compare_changes: one bounded window of the changed-file listing."""

    def test_returns_changes_and_hits_endpoint(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_change("src/app.py"), _change("docs/x.md", "ADD")],
                is_last_page=True,
            ),
        ) as mock_get:
            page = fetcher.compare_changes(
                "PROJ", "my-repo", "feature/x", to_ref="main"
            )

        assert [c.path for c in page.changes] == ["src/app.py", "docs/x.md"]
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/compare/changes"
        )
        params = mock_get.call_args[1]["params"]
        assert params["from"] == "feature/x"
        assert params["to"] == "main"
        assert params["start"] == 0

    def test_single_request_and_cursor_passthrough(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_change("a.py")], is_last_page=False, next_page_start=7
            ),
        ) as mock_get:
            page = fetcher.compare_changes("P", "r", "x", start=3, limit=1)

        assert mock_get.call_count == 1
        assert page.truncated is True
        assert page.next_page_start == 7
        assert mock_get.call_args[1]["params"]["start"] == 3

    def test_limit_is_clamped(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_changes("P", "r", "x", limit=10**6)
            fetcher.compare_changes("P", "r", "x", limit=0)

        sent = [c.kwargs["params"]["limit"] for c in mock_get.call_args_list]
        assert sent == [MAX_CHANGES_LIMIT, 1]

    def test_segments_are_encoded(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_changes("P K", "r/s", "x")

        assert mock_get.call_args[0][0].endswith(
            "/projects/P%20K/repos/r%2Fs/compare/changes"
        )

    def test_malformed_page_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response({"values": "nope"})
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.compare_changes("P", "r", "x")

    def test_not_found_names_the_refs(self):
        """A comparison 404 identifies the relevant ref inputs."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.compare_changes("P", "r", "no-such-ref")

        message = str(excinfo.value)
        assert "/compare/changes" in message
        assert "from_ref" in message
        assert "from_repo" in message
        assert "pull request" not in message

    def test_empty_comparison_is_a_complete_empty_window(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ):
            page = fetcher.compare_changes("P", "r", "main", to_ref="main")

        assert page.changes == []
        assert page.is_last_page is True
        assert page.truncated is False
        assert page.next_page_start is None

    def test_query_values_are_percent_encoded_on_the_wire(self):
        """Refs and from_repo are sent as encoded query values, not path text."""
        fetcher = BitbucketFetcher(config=_byo_config())
        params = fetcher._compare_params(
            "feat/a&b#c\r\nX: 1", "../../admin", "FORK/my repo"
        )
        prepared = fetcher._session.prepare_request(
            Request("GET", "https://bitbucket.corp.example.com/x", params=params)
        )

        assert prepared.url == (
            "https://bitbucket.corp.example.com/x"
            "?from=feat%2Fa%26b%23c%0D%0AX%3A+1&to=..%2F..%2Fadmin"
            "&fromRepo=FORK%2Fmy+repo"
        )
        assert "X" not in prepared.headers


class TestCompareCommits:
    """compare_commits: one bounded window of the commit listing."""

    def test_returns_commits_and_hits_endpoint(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_commit("aaaaaaa"), _commit("bbbbbbb")], is_last_page=True
            ),
        ) as mock_get:
            page = fetcher.compare_commits(
                "PROJ", "my-repo", "release/1.2", to_ref="refs/tags/v1.1"
            )

        assert [c.display_id for c in page.commits] == ["aaaaaaa", "bbbbbbb"]
        assert page.is_last_page is True
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/compare/commits"
        )
        params = mock_get.call_args[1]["params"]
        assert params["from"] == "release/1.2"
        assert params["to"] == "refs/tags/v1.1"

    def test_from_repo_and_cursor_are_forwarded(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session,
            "get",
            return_value=_page_response(
                [_commit("aaaaaaa")], is_last_page=False, next_page_start=1
            ),
        ) as mock_get:
            page = fetcher.compare_commits(
                "P", "r", "x", from_repo="FORK/r", start=0, limit=1
            )

        assert mock_get.call_count == 1
        assert page.next_page_start == 1
        params = mock_get.call_args[1]["params"]
        assert params["fromRepo"] == "FORK/r"
        assert params["limit"] == 1

    def test_limit_is_clamped(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_page_response([], is_last_page=True)
        ) as mock_get:
            fetcher.compare_commits("P", "r", "x", limit=10**6)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_COMMITS_LIMIT

    def test_malformed_page_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(["nope"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.compare_commits("P", "r", "x")

    def test_not_found_names_the_refs(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.compare_commits("P", "r", "x", from_repo="FORK/r")

        message = str(excinfo.value)
        assert "/compare/commits" in message
        assert "from_repo" in message


class TestCompareDiff:
    """compare_diff: whole-comparison and single-file forms, narrowing, caps."""

    def test_whole_form_hits_endpoint_and_streams(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            diff = fetcher.compare_diff("PROJ", "my-repo", "feature/x", to_ref="main")

        assert diff.total_files == 1
        assert diff.files[0].destination_path == "src/app.py"
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith(
            "/rest/api/1.0/projects/PROJ/repos/my-repo/compare/diff"
        )
        params = mock_get.call_args[1]["params"]
        assert params == {"from": "feature/x", "to": "main"}
        # The diff opts into streaming so the byte cap can apply.
        assert mock_get.call_args[1]["stream"] is True

    def test_applies_max_lines_per_file(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ):
            diff = fetcher.compare_diff("P", "r", "x", max_lines_per_file=2)

        file = diff.files[0]
        kept = sum(len(seg.lines) for hunk in file.hunks for seg in hunk.segments)
        assert kept == 2
        assert file.line_truncated is True
        assert diff.truncated is True

    def test_non_dict_response_raises(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(["nope"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.compare_diff("P", "r", "x")

    def test_applies_max_files(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        second = {"destination": {"components": ["b.py"], "name": "b.py"}, "hunks": []}
        body = {**_DIFF_BODY, "diffs": [*_DIFF_BODY["diffs"], second]}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            diff = fetcher.compare_diff("P", "r", "x", max_files=1)

        assert len(diff.files) == 1
        assert diff.total_files == 2
        assert diff.truncated is True

    def test_empty_comparison_is_an_empty_diff(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"fromHash": "a", "toHash": "a", "diffs": [], "truncated": False}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            diff = fetcher.compare_diff("P", "r", "main", to_ref="main")

        assert diff.files == []
        assert diff.total_files == 0
        assert diff.truncated is False

    def test_whole_form_accepts_bare_rest_diff_body(self):
        """The whole form also accepts the bare RestDiff the spec declares.

        The specification declares a bare RestDiff for the whole form while
        the pull-request endpoint returns the ``diffs`` envelope at runtime.
        Both shapes are read, and a bare body becomes a one-file diff.
        """
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ):
            diff = fetcher.compare_diff("P", "r", "x")

        assert diff.total_files == 1
        assert diff.files[0].source_path == "src/old.py"
        assert diff.truncated is False

    @pytest.mark.parametrize("body", [{"fromHash": "a"}, {"values": []}, [1]])
    def test_whole_form_rejects_unrecognised_body(self, body):
        """A non-empty body that is neither shape raises instead of reading as empty."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="diff"):
                fetcher.compare_diff("P", "r", "x")

    def test_context_lines_sent_and_clamped(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            fetcher.compare_diff("P", "r", "x", context_lines=3)
            fetcher.compare_diff("P", "r", "x", context_lines=-4)
            fetcher.compare_diff("P", "r", "x", context_lines=10**9)

        sent = [c.kwargs["params"]["contextLines"] for c in mock_get.call_args_list]
        assert sent == [3, 0, MAX_CONTEXT_LINES]

    def test_whitespace_ignore_all_is_normalised(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            fetcher.compare_diff("P", "r", "x", whitespace=" IGNORE-ALL ")

        assert mock_get.call_args[1]["params"]["whitespace"] == "ignore-all"

    def test_blank_whitespace_is_dropped(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_DIFF_BODY)
        ) as mock_get:
            fetcher.compare_diff("P", "r", "x", whitespace="  ")

        assert "whitespace" not in mock_get.call_args[1]["params"]

    def test_unrecognised_whitespace_rejected_before_request(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="whitespace must be one of"):
                fetcher.compare_diff("P", "r", "x", whitespace="ignore-some")
        mock_get.assert_not_called()

    def test_path_selects_single_file_form(self):
        """A path goes into the URL and the bare RestDiff is wrapped as one file."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ) as mock_get:
            diff = fetcher.compare_diff("P", "r", "x", path="dir name/a#b.py")

        assert mock_get.call_args[0][0].endswith(
            "/projects/P/repos/r/compare/diff/dir%20name/a%23b.py"
        )
        assert diff.total_files == 1
        assert diff.files[0].destination_path == "src/app.py"
        assert diff.files[0].source_path == "src/old.py"

    @pytest.mark.parametrize("bad", ["../etc/passwd", "src//app.py", "./a.py"])
    def test_path_traversal_rejected_before_request(self, bad):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="traversal"):
                fetcher.compare_diff("P", "r", "x", path=bad)
        mock_get.assert_not_called()

    def test_src_path_sent_as_query_and_requires_path(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_SINGLE_FILE_DIFF)
        ) as mock_get:
            fetcher.compare_diff(
                "P", "r", "x", path="src/app.py", src_path=" src/old.py "
            )
            assert mock_get.call_args[1]["params"]["srcPath"] == "src/old.py"

            with pytest.raises(ValueError, match="src_path requires path"):
                fetcher.compare_diff("P", "r", "x", src_path="src/old.py")
            with pytest.raises(ValueError, match="src_path"):
                fetcher.compare_diff("P", "r", "x", path="a.py", src_path="../b.py")
        assert mock_get.call_count == 1

    def test_single_file_form_accepts_the_diffs_envelope(self):
        """A wrapped body on the path form is unwrapped rather than misread."""
        fetcher = BitbucketFetcher(config=_byo_config())
        body = {"diffs": [_SINGLE_FILE_DIFF]}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            diff = fetcher.compare_diff("P", "r", "x", path="src/app.py")

        assert diff.total_files == 1
        assert diff.files[0].destination_path == "src/app.py"

    @pytest.mark.parametrize("body", [{}, {"size": 0}, {"lines": []}])
    def test_single_file_form_rejects_unrecognised_body(self, body):
        """A body that is neither a RestDiff nor a diffs envelope raises."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="unexpected diff response shape"):
                fetcher.compare_diff("P", "r", "x", path="src/app.py")

    def test_single_file_not_found_names_the_path(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.compare_diff("P", "r", "x", path="gone.py")

        message = str(excinfo.value)
        assert "'gone.py'" in message
        assert "src_path" in message

    def test_whole_form_not_found_names_the_refs(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(
            fetcher._session, "get", return_value=_http_error_response(404)
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.compare_diff("P", "r", "no-such-ref")

        message = str(excinfo.value)
        assert "/compare/diff" in message
        assert "from_ref" in message
        assert "src_path" not in message

    def test_oversize_error_names_the_compare_recovery_actions(self):
        """Whole-comparison recovery guidance names the compare tools."""
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get", return_value=_oversized_response()):
            with pytest.raises(BitbucketResponseTooLargeError) as excinfo:
                fetcher.compare_diff("P", "r", "x")

        message = str(excinfo.value)
        assert "download cap" in message
        assert "bitbucket_compare_changes" in message
        assert "bitbucket_compare_diff" in message
        assert "context_lines" in message
        assert "pull_request" not in message

    def test_single_file_oversize_error_names_only_context_lines(self):
        fetcher = BitbucketFetcher(config=_byo_config())
        with patch.object(fetcher._session, "get", return_value=_oversized_response()):
            with pytest.raises(BitbucketResponseTooLargeError) as excinfo:
                fetcher.compare_diff("P", "r", "x", path="a.py")

        message = str(excinfo.value)
        assert "context_lines" in message
        assert "bitbucket_compare_changes" not in message
