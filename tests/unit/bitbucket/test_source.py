"""Unit tests for SourceMixin (_encode_browse_path, browse)."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.source import (
    MAX_BROWSE_LIMIT,
    MAX_BROWSE_RESPONSE_BYTES,
)
from mcp_atlassian.models.bitbucket import BitbucketDirectoryEntry
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import attach_json


def _json_response(body):
    """Build a mock response carrying a JSON body."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    attach_json(response, body)
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
        # A Windows-style "a\..\b" is one literal component with the backslashes
        # percent-encoded, so the encoder does not split on backslash and
        # cannot reopen backslash traversal.
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

    def test_directory_missing_is_last_page_raises(self):
        """A children object without isLastPage is a response-shape error."""
        fetcher = _fetcher()
        body = {"children": {"values": []}}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="boolean 'isLastPage'"):
                fetcher.browse("P", "r", path="src")

    def test_directory_string_is_last_page_raises(self):
        """The string "false" is not a boolean and raises."""
        fetcher = _fetcher()
        body = _dir_body([], is_last_page="false", next_page_start=25)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="boolean 'isLastPage'"):
                fetcher.browse("P", "r", path="src")

    @pytest.mark.parametrize("cursor", [None, 25, 10, "50"])
    def test_directory_unusable_cursor_is_not_resumable(self, cursor):
        """A non-last page whose cursor is missing, non-integer, or not
        beyond the requested start is reported truncated with no cursor."""
        fetcher = _fetcher()
        body = _dir_body([], is_last_page=False, next_page_start=cursor)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="src", start=25)

        assert result.is_last_page is False
        assert result.truncated is True
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

    def test_file_missing_is_last_page_raises(self):
        fetcher = _fetcher()
        body = {"lines": [{"text": "a"}]}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="boolean 'isLastPage'"):
                fetcher.browse("P", "r", path="big.txt")

    def test_file_non_advancing_cursor_is_not_resumable(self):
        fetcher = _fetcher()
        body = _file_body(["a"], is_last_page=False, next_page_start=100)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="big.txt", start=100)

        assert result.truncated is True
        assert result.next_page_start is None

    def test_binary_body_with_lines_and_no_is_last_page(self):
        """The binary flag governs cursor tolerance whether or not a `lines`
        array rides along; the payload is not read."""
        fetcher = _fetcher()
        body = {"binary": True, "lines": [{"text": "x"}]}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="img.png")

        assert result.binary is True
        assert result.lines == []
        assert result.is_last_page is True
        assert result.next_page_start is None

    def test_root_path_is_quoted_in_cursor_error(self):
        fetcher = _fetcher()
        body = {"children": {"values": []}}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="for ''; expected a boolean"):
                fetcher.browse("P", "r")

    def test_binary_body_with_string_is_last_page_raises(self):
        fetcher = _fetcher()
        body = {"binary": True, "isLastPage": "true"}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="boolean 'isLastPage'"):
                fetcher.browse("P", "r", path="img.png")

    def test_non_object_line_entry_raises(self):
        """A non-object `lines` entry is a response-shape error."""
        fetcher = _fetcher()
        body = {"lines": [{"text": "ok"}, "junk"], "isLastPage": True}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="object with a string 'text'"):
                fetcher.browse("P", "r", path="f.txt")

    @pytest.mark.parametrize("bad_line", [{"no_text": 1}, {"text": None}, {"text": 7}])
    def test_non_string_line_text_raises(self, bad_line):
        """A line whose `text` is missing or not a string is a shape error."""
        fetcher = _fetcher()
        body = {"lines": [{"text": "ok"}, bad_line], "isLastPage": True}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="object with a string 'text'"):
                fetcher.browse("P", "r", path="f.txt")

    def test_empty_line_text_is_preserved(self):
        fetcher = _fetcher()
        body = {"lines": [{"text": "a"}, {"text": ""}], "isLastPage": True}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="f.txt")

        assert result.lines == ["a", ""]

    def test_binary_file_does_not_forward_raw_bytes(self):
        fetcher = _fetcher()
        # A binary body: the 'binary' flag is the signal, and the raw content that
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

    def test_binary_body_without_lines_key(self, caplog):
        """The live binary body carries only `{binary, path}` and no `lines`."""
        fetcher = _fetcher()
        body = {
            "binary": True,
            "path": {"components": ["img.png"], "name": "img.png"},
        }
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="img.png")

        assert result.kind == "FILE"
        assert result.binary is True
        assert result.lines == []
        assert result.children is None
        assert result.is_last_page is True
        assert result.truncated is False
        # A recognised shape: no "unexpected shape" warning is logged.
        assert "Unexpected browse response shape" not in caplog.text


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
    """Empty bodies are empty results; malformed bodies raise."""

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

    def test_unrecognised_non_empty_body_raises(self):
        """A non-empty body matching no browse shape raises an error."""
        fetcher = _fetcher()
        body = {"unexpected": "shape"}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(
                ValueError, match="neither 'children' nor 'lines' nor a 'binary'"
            ):
                fetcher.browse("P", "r", path="x")

    def test_empty_body_is_an_empty_file(self):
        """A completely empty body is accepted as an empty file."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_json_response({})):
            result = fetcher.browse("P", "r", path="x")

        assert result.kind == "FILE"
        assert result.lines == []
        assert result.children is None
        assert result.is_last_page is True
        assert result.truncated is False

    def test_children_present_but_not_a_dict_raises(self):
        """'children' present but a list (drifted/malformed) raises a
        response-shape error."""
        fetcher = _fetcher()
        body = {"children": [{"type": "FILE"}]}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="'children' is list"):
                fetcher.browse("P", "r", path="x")

    @pytest.mark.parametrize("bad_values", [None, "x", {"a": 1}])
    def test_children_values_not_a_list_raises(self, bad_values):
        """A `children` object whose `values` is missing or not a list is a
        response-shape error rather than an empty directory."""
        fetcher = _fetcher()
        body = {"children": {"isLastPage": True}}
        if bad_values is not None:
            body["children"]["values"] = bad_values
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="'children.values' is"):
                fetcher.browse("P", "r", path="x")

    def test_non_object_child_entry_raises(self):
        """A non-object entry in `children.values` is a response-shape error."""
        fetcher = _fetcher()
        body = _dir_body([{"type": "FILE", "path": {"toString": "a"}}, "junk"])
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="'children.values' entry"):
                fetcher.browse("P", "r", path="x")

    def test_null_lines_is_treated_like_absent(self):
        """A null 'lines' is absent; a body with nothing else is a shape error."""
        fetcher = _fetcher()
        body = {"lines": None}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(ValueError, match="neither 'children' nor 'lines'"):
                fetcher.browse("P", "r", path="x")

    @pytest.mark.parametrize("lines", ["not-a-list", {"text": "x"}])
    def test_lines_present_but_not_a_list_raises(self, lines):
        """A present 'lines' that is not a list is named in the shape error."""
        fetcher = _fetcher()
        body = {"lines": lines}
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            with pytest.raises(
                ValueError, match=f"'lines' is {type(lines).__name__}, not a list"
            ):
                fetcher.browse("P", "r", path="x")

    def test_non_dict_body_raises(self):
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", return_value=_json_response(["nope"])
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.browse("P", "r", path="x")


class TestBrowseBounds:
    """The browse window is bounded by the requested limit and by bytes."""

    def test_directory_overshoot_is_trimmed_without_cursor(self):
        """A server ignoring ``limit`` has its surplus children trimmed.

        The trimmed tail lives inside this window, so no resume cursor is
        advertised and the result is marked truncated.
        """
        fetcher = _fetcher()
        values = [{"path": {"components": [f"f{i}"]}} for i in range(5)]
        body = _dir_body(values, is_last_page=False, next_page_start=5)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="src", limit=2)

        assert [c.path for c in result.children] == ["f0", "f1"]
        assert result.truncated is True
        assert result.next_page_start is None

    def test_file_overshoot_is_trimmed_without_cursor(self):
        fetcher = _fetcher()
        body = _file_body(["a", "b", "c", "d"], is_last_page=False, next_page_start=4)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="f.txt", limit=3)

        assert result.lines == ["a", "b", "c"]
        assert result.truncated is True
        assert result.next_page_start is None

    def test_exact_window_keeps_cursor(self):
        """A window of exactly ``limit`` items keeps the upstream cursor."""
        fetcher = _fetcher()
        body = _file_body(["a", "b"], is_last_page=False, next_page_start=2)
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="f.txt", limit=2)

        assert result.lines == ["a", "b"]
        assert result.truncated is True
        assert result.next_page_start == 2

    def test_oversized_declared_body_aborts_before_download(self):
        """A Content-Length above the browse byte cap raises without reading."""
        fetcher = _fetcher()
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Length": str(MAX_BROWSE_RESPONSE_BYTES + 1)}
        with patch.object(fetcher._session, "get", return_value=response) as mock_get:
            with pytest.raises(ValueError, match="download cap"):
                fetcher.browse("P", "r", path="huge.min.js")

        assert mock_get.call_args[1]["stream"] is True
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_oversized_chunked_body_aborts_mid_download(self):
        """A chunked body with one very long line stops at the byte cap."""
        fetcher = _fetcher()
        chunk = b"x" * 65536
        chunk_count = MAX_BROWSE_RESPONSE_BYTES // len(chunk) + 2
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.headers = {}
        response.iter_content.return_value = iter([chunk] * chunk_count)
        with patch.object(fetcher._session, "get", return_value=response):
            with pytest.raises(ValueError, match="download cap"):
                fetcher.browse("P", "r", path="huge.min.js")

        response.close.assert_called_once()

    @pytest.mark.parametrize("bad_cursor", ["25", 25.0, True, None])
    def test_misshaped_cursor_is_dropped(self, bad_cursor):
        """A non-integer nextPageStart is reported as not resumable."""
        fetcher = _fetcher()
        body = _file_body(["a"], is_last_page=False)
        body["nextPageStart"] = bad_cursor
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="f.txt", limit=1)

        assert result.truncated is True
        assert result.next_page_start is None

    def test_non_advancing_cursor_is_dropped(self):
        """A cursor at or before the current offset would refetch the window."""
        fetcher = _fetcher()
        body = _dir_body(
            [{"path": {"components": ["a"]}}], is_last_page=False, next_page_start=10
        )
        with patch.object(fetcher._session, "get", return_value=_json_response(body)):
            result = fetcher.browse("P", "r", path="src", start=10, limit=1)

        assert result.truncated is True
        assert result.next_page_start is None
