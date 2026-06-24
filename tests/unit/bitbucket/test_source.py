"""Unit tests for SourceMixin (_encode_browse_path, browse)."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.source import MAX_BROWSE_LIMIT
from mcp_atlassian.models.bitbucket import BitbucketDirectoryEntry
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig


def _json_response(body):
    """Build a mock response carrying a JSON body."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def _fetcher() -> BitbucketFetcher:
    """Build a DC BYO-token fetcher for source tests."""
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


def _dir_body(values, is_last_page=True, next_page_start=None):
    """Build a browse directory body."""
    children = {"values": values, "isLastPage": is_last_page}
    if next_page_start is not None:
        children["nextPageStart"] = next_page_start
    return {"children": children}


def _file_body(texts, is_last_page=True, next_page_start=None, binary=None):
    """Build a browse file body."""
    body = {"lines": [{"text": t} for t in texts], "isLastPage": is_last_page}
    if next_page_start is not None:
        body["nextPageStart"] = next_page_start
    if binary is not None:
        body["binary"] = binary
    return body


def _encode(path):
    """Call the static path encoder directly."""
    return BitbucketFetcher._encode_browse_path(path)


class TestEncodeBrowsePath:
    """The security-critical path encoder."""

    def test_empty_input_is_repo_root(self):
        assert _encode("") == ""
        assert _encode("   ") == ""

    def test_preserves_nested_slashes(self):
        assert _encode("src/app.py") == "src/app.py"

    def test_percent_encodes_space(self):
        assert _encode("my dir/file.py") == "my%20dir/file.py"

    def test_percent_encodes_hash(self):
        # '#' would otherwise be read as a URL fragment separator.
        assert _encode("a#b/c") == "a%23b/c"

    def test_percent_encodes_unicode(self):
        assert _encode("café/x") == "caf%C3%A9/x"

    def test_backslash_traversal_is_a_single_encoded_component(self):
        # A Windows-style "a\..\b" must NOT be split on backslash or traversed:
        # it stays one literal component with the backslashes percent-encoded,
        # so the encoder can never silently reopen backslash traversal.
        assert _encode("a\\..\\b") == "a%5C..%5Cb"

    @pytest.mark.parametrize(
        "bad",
        ["..", "../etc/passwd", "src/../secret", "a/..", "..\\x".replace("\\", "/")],
    )
    def test_rejects_parent_traversal(self, bad):
        with pytest.raises(ValueError, match="traversal"):
            _encode(bad)

    def test_rejects_dot_component(self):
        with pytest.raises(ValueError, match="traversal"):
            _encode("a/./b")

    @pytest.mark.parametrize("bad", ["a//b", "/a", "a/", "//"])
    def test_rejects_empty_component(self, bad):
        with pytest.raises(ValueError, match="traversal"):
            _encode(bad)

    def test_traversal_rejected_before_any_request(self):
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get") as mock_get:
            with pytest.raises(ValueError, match="traversal"):
                fetcher.browse("PROJ", "my-repo", path="../etc")
        assert mock_get.call_count == 0


class TestBrowseDirectory:
    """browse() over a directory body."""

    def test_returns_entries_with_joined_paths(self):
        fetcher = _fetcher()
        body = _dir_body(
            [
                {
                    "path": {"components": ["src", "app.py"], "name": "app.py"},
                    "type": "FILE",
                    "size": 12,
                    "contentId": "blob1",
                },
                {
                    "path": {"components": ["src", "lib"], "name": "lib"},
                    "type": "DIRECTORY",
                },
            ]
        )
        with patch.object(
            fetcher._session, "get", return_value=_json_response(body)
        ) as mock_get:
            result = fetcher.browse("PROJ", "my-repo", path="src")

        assert mock_get.call_count == 1
        assert result.kind == "DIRECTORY"
        assert result.lines is None
        assert result.children is not None
        assert all(isinstance(e, BitbucketDirectoryEntry) for e in result.children)
        assert result.children[0].path == "src/app.py"
        assert result.children[0].type == "FILE"
        assert result.children[0].content_id == "blob1"
        assert result.children[1].path == "src/lib"

    def test_directory_cursor_surfaced(self):
        fetcher = _fetcher()
        body = _dir_body([], is_last_page=False, next_page_start=25)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="src")

        assert result.is_last_page is False
        assert result.truncated is True
        assert result.next_page_start == 25

    def test_directory_last_page_nulls_cursor(self):
        fetcher = _fetcher()
        body = _dir_body([], is_last_page=True, next_page_start=99)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="src")

        assert result.is_last_page is True
        assert result.truncated is False
        assert result.next_page_start is None


class TestBrowseFile:
    """browse() over a file body."""

    def test_extracts_lines(self):
        fetcher = _fetcher()
        body = _file_body(["import os", "print(os.getcwd())"])
        with patch.object(
            fetcher._session, "get", return_value=_json_response(body)
        ) as mock_get:
            result = fetcher.browse("PROJ", "my-repo", path="src/app.py")

        assert mock_get.call_count == 1
        assert result.kind == "FILE"
        assert result.children is None
        assert result.lines == ["import os", "print(os.getcwd())"]
        assert result.binary is False

    def test_surfaces_binary_flag(self):
        fetcher = _fetcher()
        body = _file_body([], binary=True)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="img.png")

        assert result.kind == "FILE"
        assert result.binary is True

    def test_file_cursor_surfaced(self):
        fetcher = _fetcher()
        body = _file_body(["a"], is_last_page=False, next_page_start=100)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="big.txt")

        assert result.is_last_page is False
        assert result.truncated is True
        assert result.next_page_start == 100

    def test_malformed_lines_filtered(self):
        fetcher = _fetcher()
        body = {"lines": [{"text": "ok"}, "junk", {"no_text": 1}], "isLastPage": True}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="f.txt")

        # Non-dict entries are dropped; a dict without 'text' yields "".
        assert result.lines == ["ok", ""]

    def test_null_text_becomes_empty_string_not_literal_none(self):
        fetcher = _fetcher()
        # A present-but-null 'text' must become "" — not the string "None".
        body = {"lines": [{"text": "ok"}, {"text": None}], "isLastPage": True}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="f.txt")

        assert result.lines == ["ok", ""]

    def test_binary_file_does_not_forward_raw_bytes(self):
        fetcher = _fetcher()
        # A binary body: the 'binary' flag is the signal — the raw content that
        # may ride in lines[].text is not extracted or forwarded.
        body = {
            "binary": True,
            "lines": [{"text": "<rawbytes>"}],
            "isLastPage": True,
        }
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="img.png")

        assert result.binary is True
        assert result.lines == []


class TestBrowseRequestShape:
    """URL construction, params, and the single-GET invariant."""

    def test_repo_root_url_has_no_path_suffix(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_dir_body([]))
        ) as mock_get:
            fetcher.browse("PROJ", "my-repo")

        url = mock_get.call_args[0][0]
        assert url.endswith("/rest/api/1.0/projects/PROJ/repos/my-repo/browse")

    def test_path_appended_to_browse_url(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_file_body(["x"]))
        ) as mock_get:
            fetcher.browse("PROJ", "my-repo", path="src/app.py")

        url = mock_get.call_args[0][0]
        assert url.endswith("/projects/PROJ/repos/my-repo/browse/src/app.py")

    def test_at_start_limit_land_in_params(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_file_body(["x"]))
        ) as mock_get:
            fetcher.browse("P", "r", path="f", at="main", start=10, limit=50)

        params = mock_get.call_args[1]["params"]
        assert params["at"] == "main"
        assert params["start"] == 10
        assert params["limit"] == 50

    def test_at_omitted_when_blank(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_file_body(["x"]))
        ) as mock_get:
            fetcher.browse("P", "r", path="f", at="  ")

        assert "at" not in mock_get.call_args[1]["params"]

    def test_limit_clamped_to_max(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_file_body(["x"]))
        ) as mock_get:
            fetcher.browse("P", "r", path="f", limit=MAX_BROWSE_LIMIT + 5000)

        assert mock_get.call_args[1]["params"]["limit"] == MAX_BROWSE_LIMIT

    def test_single_get_only(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(_dir_body([]))
        ) as mock_get:
            fetcher.browse("P", "r", path="src")

        assert mock_get.call_count == 1


class TestBrowseDefensive:
    """Empty/unrecognised shapes never raise."""

    def test_empty_file_returns_empty_lines_no_warning(self, caplog):
        fetcher = _fetcher()
        body = {"lines": [], "isLastPage": True}
        with caplog.at_level(logging.WARNING):
            with patch.object(
                fetcher._session, "get", return_value=_json_response(body)
            ):
                result = fetcher.browse("P", "r", path="empty.txt")

        assert result.kind == "FILE"
        assert result.lines == []
        assert "Unexpected browse response shape" not in caplog.text

    def test_empty_directory_returns_empty_children_no_warning(self, caplog):
        fetcher = _fetcher()
        with caplog.at_level(logging.WARNING):
            with patch.object(
                fetcher._session, "get", return_value=_json_response(_dir_body([]))
            ):
                result = fetcher.browse("P", "r", path="emptydir")

        assert result.kind == "DIRECTORY"
        assert result.children == []
        assert "Unexpected browse response shape" not in caplog.text

    def test_unrecognised_body_warns_and_returns_empty(self, caplog):
        fetcher = _fetcher()
        body = {"unexpected": "shape"}
        with caplog.at_level(logging.WARNING):
            with patch.object(
                fetcher._session, "get", return_value=_json_response(body)
            ):
                result = fetcher.browse("P", "r", path="x")

        assert result.kind == "FILE"
        assert result.lines == []
        assert result.children is None
        assert "Unexpected browse response shape" in caplog.text

    def test_children_present_but_not_a_dict_warns_and_not_a_file(self, caplog):
        fetcher = _fetcher()
        # 'children' present but a list (drifted/malformed) must not silently
        # fall through and be misclassified as a populated FILE.
        body = {"children": [{"type": "FILE"}]}
        with caplog.at_level(logging.WARNING):
            with patch.object(
                fetcher._session, "get", return_value=_json_response(body)
            ):
                result = fetcher.browse("P", "r", path="x")

        assert "'children' present but is list" in caplog.text
        assert result.lines == []
        assert result.children is None

    def test_non_dict_body_raises(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(["nope"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.browse("P", "r", path="x")
