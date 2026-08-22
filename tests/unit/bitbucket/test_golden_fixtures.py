"""Golden-fixture regression tests for the Bitbucket read surface.

The inline wire-shape guards in tests/unit/models encode the shapes the
models must tolerate; these tests replay the same parse paths against the
full golden envelopes (tests/fixtures/bitbucket/), so a wire shape that
drifts from the inline assumptions fails here instead of at runtime.

The corpus is fully synthetic; the hygiene tests below enforce the
synthetic-value invariants described in the fixture README.
"""

import json
import re
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.models.bitbucket import (
    BitbucketActivity,
    BitbucketBranch,
    BitbucketProject,
    BitbucketPullRequest,
    BitbucketPullRequestDiff,
    BitbucketRepository,
)
from mcp_atlassian.utils.logging import mask_sensitive
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.fixtures.bitbucket_golden import (
    GOLDEN_ROOT,
    PR_SURFACE_EDITIONS,
    load_golden,
)
from tests.unit.bitbucket.mock_responses import attach_json


def _fetcher() -> BitbucketFetcher:
    """Build a fetcher with a BYO-token config (no network on construction)."""
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


def _response(body: dict) -> MagicMock:
    """Build a mock response carrying a golden JSON body."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    attach_json(response, body)
    return response


_GOLDEN_PATHS = sorted(GOLDEN_ROOT.glob("*/*.json"))


class TestGoldenFixtureHygiene:
    """The committed corpus stays synthetic.

    A self-enforcing floor for fixture additions: hosts and emails sit under
    the reserved example.com domain, git object IDs carry the ``f1ce`` marker
    prefix, timestamps fall in the 2021 window, and activity, comment, and
    user IDs sit in their reserved ranges. Assertion messages are masked so a
    failure does not echo the offending value into retained CI logs.
    """

    #: Marker prefix carried by every synthetic git object ID.
    HASH_PREFIX = "f1ce"
    #: Key suffixes whose integer values are timestamps (epoch ms or s).
    TIMESTAMP_KEY_SUFFIXES = ("Date", "Timestamp")
    #: Calendar year 2021, in epoch seconds.
    YEAR_START_S = 1_609_459_200
    YEAR_END_S = 1_640_995_200
    #: Reserved synthetic ID ranges.
    ACTIVITY_IDS = range(710_001, 720_000)
    COMMENT_IDS = range(720_001, 730_000)
    USER_IDS = range(730_001, 740_000)

    @staticmethod
    def _is_synthetic(domain: str) -> bool:
        return domain == "example.com" or domain.endswith(".example.com")

    @classmethod
    def _walk(cls, node: object) -> Iterator[dict[str, Any]]:
        """Yield every dict in a parsed fixture body, depth-first."""
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from cls._walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from cls._walk(value)

    def test_corpus_is_present(self):
        """Guard the parametrised scan against silently going empty."""
        assert len(_GOLDEN_PATHS) >= 15

    @pytest.mark.parametrize(
        "fixture_path", _GOLDEN_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}"
    )
    def test_hosts_and_emails_are_synthetic(self, fixture_path):
        text = fixture_path.read_text()
        # Matches both mail addresses and the user@host part of URLs on any
        # scheme; either way the domain must be under reserved example.com.
        for address in re.findall(r"[\w.+-]+@[\w.-]+", text):
            domain = address.rsplit("@", 1)[1]
            assert self._is_synthetic(domain), mask_sensitive(domain, 2)
        # Dotted host required so path fragments after a doubled slash
        # (e.g. inside fixture file content) don't false-positive.
        for host in re.findall(
            r"(?:[a-z][a-z0-9+.-]*:)?//(?:[\w.+-]+@)?([\w-]+(?:\.[\w-]+)+)(?::\d+)?",
            text,
        ):
            assert self._is_synthetic(host), mask_sensitive(host, 2)

    @pytest.mark.parametrize(
        "fixture_path", _GOLDEN_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}"
    )
    def test_git_object_ids_are_synthetic(self, fixture_path):
        """Every hash-like hex token carries the marker prefix.

        Matches full and abbreviated git object IDs (commit/blob hashes and
        display IDs) in either case. A short token needs both a digit and a
        hex letter so plain numbers and ordinary words do not false-positive;
        a letter-only hex token of 12+ characters is hash-like on its own.
        """
        text = fixture_path.read_text()
        for token in re.findall(
            r"(?<![0-9a-fA-F])[0-9a-fA-F]{7,}(?![0-9a-fA-F])", text
        ):
            has_letter = re.search(r"[a-fA-F]", token)
            has_digit = re.search(r"\d", token)
            if has_letter and (has_digit or len(token) >= 12):
                assert token.lower().startswith(self.HASH_PREFIX), mask_sensitive(
                    token, 4
                )

    #: The one deliberate id/displayId mismatch, kept to exercise parse
    #: tolerance of abbreviation drift (see the fixture README).
    PREFIX_EXCEPTIONS = frozenset({("pr_commits", "f1ce472e7f0")})

    @pytest.mark.parametrize(
        "fixture_path", _GOLDEN_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}"
    )
    def test_display_ids_abbreviate_their_id(self, fixture_path):
        """Every abbreviated displayId is a prefix of its full object ID."""
        body = json.loads(fixture_path.read_text())
        for node in self._walk(body):
            object_id, display_id = node.get("id"), node.get("displayId")
            if (
                not isinstance(object_id, str)
                or not isinstance(display_id, str)
                or object_id.startswith("refs/")
                or len(display_id) >= len(object_id)
            ):
                continue
            if (fixture_path.stem, display_id) in self.PREFIX_EXCEPTIONS:
                continue
            assert object_id.startswith(display_id), mask_sensitive(display_id, 4)

    @pytest.mark.parametrize(
        "fixture_path", _GOLDEN_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}"
    )
    def test_timestamps_are_synthetic(self, fixture_path):
        """Every timestamp falls in the reserved 2021 window (ms or s)."""
        body = json.loads(fixture_path.read_text())
        for node in self._walk(body):
            for key, value in node.items():
                if not key.endswith(self.TIMESTAMP_KEY_SUFFIXES) or not isinstance(
                    value, int
                ):
                    continue
                seconds = value // 1000 if value >= 10**12 else value
                assert self.YEAR_START_S <= seconds < self.YEAR_END_S, key

    @pytest.mark.parametrize(
        "fixture_path", _GOLDEN_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}"
    )
    def test_activity_and_comment_ids_are_synthetic(self, fixture_path):
        """Activity and comment IDs sit in their reserved ranges.

        Comment-like nodes (comments, threaded replies, and tasks, meaning any
        dict carrying both ``text`` and an integer ``id``) share the comment
        range; top-level activity comments are additionally ascending.
        """
        body = json.loads(fixture_path.read_text())
        activity_ids: list[int] = []
        comment_ids: list[int] = []
        for node in self._walk(body):
            if "action" in node and isinstance(node.get("id"), int):
                activity_ids.append(node["id"])
            comment = node.get("comment")
            if isinstance(comment, dict) and isinstance(comment.get("id"), int):
                comment_ids.append(comment["id"])
            if "text" in node and isinstance(node.get("id"), int):
                assert node["id"] in self.COMMENT_IDS
        assert all(i in self.ACTIVITY_IDS for i in activity_ids)
        assert activity_ids == sorted(activity_ids)
        assert all(i in self.COMMENT_IDS for i in comment_ids)
        assert comment_ids == sorted(comment_ids)

    @pytest.mark.parametrize(
        "fixture_path", _GOLDEN_PATHS, ids=lambda p: f"{p.parent.name}/{p.stem}"
    )
    def test_user_ids_are_synthetic(self, fixture_path):
        """Every user object's numeric ID sits in the reserved range."""
        body = json.loads(fixture_path.read_text())
        for node in self._walk(body):
            if "slug" in node and "active" in node and isinstance(node.get("id"), int):
                assert node["id"] in self.USER_IDS, node["slug"]


class TestGoldenPullRequestSurface:
    """The pull-request-surface fixtures, parsed at the model layer."""

    @pytest.mark.parametrize("edition", PR_SURFACE_EDITIONS)
    def test_projects_parse(self, edition):
        values = load_golden(edition, "projects")["values"]
        projects = [BitbucketProject.from_api_response(v) for v in values]
        assert len(projects) == len(values) > 0
        assert all(p.key for p in projects)

    @pytest.mark.parametrize("edition", PR_SURFACE_EDITIONS)
    def test_repositories_parse(self, edition):
        values = load_golden(edition, "repos")["values"]
        repos = [BitbucketRepository.from_api_response(v) for v in values]
        assert len(repos) == len(values) > 0
        assert all(r.slug and r.project is not None for r in repos)

    def test_repositories_drop_unmodelled_fields(self):
        """Wire keys the model does not pick stay out of the projection.

        This body carries keys beyond the spec-confirmed 9.x repo additions
        (e.g. ``scalable``, which the spec does not confirm for this shape);
        all unmodelled keys are dropped.
        """
        raw = load_golden("bb9", "repos")["values"][0]
        repo = BitbucketRepository.from_api_response(raw)
        assert repo.slug == raw["slug"]
        assert "scalable" not in repo.to_simplified_dict()

    @pytest.mark.parametrize("edition", PR_SURFACE_EDITIONS)
    def test_pr_get_surfaces_top_level_author(self, edition):
        pr = BitbucketPullRequest.from_api_response(load_golden(edition, "pr_get"))
        assert pr.author is not None
        assert pr.author.role == "AUTHOR"
        assert pr.author.user is not None and pr.author.user.name
        assert all(p.role != "AUTHOR" for p in pr.participants)

    def test_pr_list_parses_through_the_fetcher(self):
        """Envelope keys beyond values/isLastPage/nextPageStart are ignored.

        This body carries a Cloud-style extra (``pagelen``) absent from the
        DC envelope; the paginator must not read it.
        """
        body = load_golden("bb9", "pr_list")
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_response(body)):
            page = fetcher.list_pull_requests("PROJ", "my-repo")
        assert len(page.pull_requests) == len(body["values"])
        assert page.pull_requests[0].author is not None
        assert page.is_last_page is True
        assert page.next_page_start is None

    @pytest.mark.parametrize("edition", PR_SURFACE_EDITIONS)
    def test_pr_diff_reads_diffs_key(self, edition):
        diff = BitbucketPullRequestDiff.from_api_response(
            load_golden(edition, "pr_diff")
        )
        assert diff.total_files == len(diff.files) > 0

    @pytest.mark.parametrize("edition", PR_SURFACE_EDITIONS)
    def test_pr_activities_parse(self, edition):
        values = load_golden(edition, "pr_activities")["values"]
        activities = [BitbucketActivity.from_api_response(v) for v in values]
        assert len(activities) == len(values) > 0
        assert all(a.action for a in activities)


class TestGoldenRepositorySurface:
    """The branch/tag/commit/browse fixtures, replayed at the wire."""

    def test_branches(self):
        body = load_golden("bb9", "branches")
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_response(body)):
            page = fetcher.list_branches("PROJ", "my-repo")
        assert len(page.branches) == len(body["values"])
        default = next(b for b in page.branches if b.is_default)
        assert default.display_id == "main"
        assert default.latest_commit
        # The non-default branch reports its explicit `isDefault: false`.
        non_default = next(b for b in page.branches if b is not default)
        assert non_default.is_default is False
        assert non_default.to_simplified_dict()["is_default"] is False
        # The legacy `latestChangeset` alias is present on the wire but dropped.
        assert "latestChangeset" in body["values"][0]
        assert "latest_changeset" not in default.to_simplified_dict()
        assert page.is_last_page is True
        assert page.next_page_start is None

    def test_tags_empty_page(self):
        """The empty envelope of a repository with no tags round-trips."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session,
            "get",
            return_value=_response(load_golden("bb9", "tags")),
        ):
            page = fetcher.list_tags("PROJ", "my-repo")
        assert page.tags == []
        assert page.is_last_page is True
        assert page.truncated is False

    def test_default_branch_minimal_ref(self):
        """`/default-branch` returns a RestMinimalRef with no SHA and no isDefault."""
        body = load_golden("bb9", "default_branch")
        assert "latestCommit" not in body  # the golden body really is minimal
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_response(body)):
            branch = fetcher.get_default_branch("PROJ", "my-repo")
        assert branch.display_id == "main"
        assert branch.id == "refs/heads/main"
        assert branch.latest_commit is None

    def test_default_branch_legacy_full_shape(self):
        """The legacy `/branches/default` body carries the full branch shape."""
        branch = BitbucketBranch.from_api_response(
            load_golden("bb9", "default_branch_legacy")
        )
        assert branch.display_id == "main"
        assert branch.latest_commit
        assert branch.is_default is True
        assert "latest_changeset" not in branch.to_simplified_dict()

    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("commits", lambda f: f.list_commits("PROJ", "my-repo")),
            (
                "pr_commits",
                lambda f: f.get_pull_request_commits("PROJ", "my-repo", 1),
            ),
        ],
    )
    def test_commit_listings(self, name, call):
        """Commit bodies carry inline author objects and epoch-ms timestamps."""
        body = load_golden("bb9", name)
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_response(body)):
            page = call(fetcher)
        assert len(page.commits) == len(body["values"]) > 0
        for commit, raw in zip(page.commits, body["values"], strict=True):
            assert commit.id == raw["id"]
            assert commit.author is not None and commit.author.name
            assert isinstance(commit.author_timestamp, int)
            assert commit.author_timestamp > 10**12  # epoch-ms, not seconds
            # The inline wire email is present on the wire and absent from the output.
            assert "emailAddress" in raw["author"]
            assert "emailAddress" not in str(commit.to_simplified_dict())

    def test_browse_file_window_with_cursor(self):
        """A windowed file read carries a resume cursor on the wire."""
        body = load_golden("bb9", "browse_file")
        assert body["isLastPage"] is False  # the body is a truncated window
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_response(body)):
            result = fetcher.browse("PROJ", "my-repo", path="README.md")
        assert result.kind == "FILE"
        assert result.children is None
        assert result.lines  # text lines extracted from the wire objects
        assert result.binary is False
        assert result.is_last_page is False
        assert result.truncated is True
        assert result.next_page_start == body["nextPageStart"]

    def test_browse_directory(self):
        body = load_golden("bb9", "browse_dir")
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_response(body)):
            result = fetcher.browse("PROJ", "my-repo")
        assert result.kind == "DIRECTORY"
        assert result.lines is None
        assert len(result.children) == len(body["children"]["values"])
        assert all(c.path and c.type for c in result.children)
        assert result.is_last_page is True

    def test_browse_binary_file(self):
        """A binary file body carries only `{binary, path}` and no lines."""
        body = load_golden("bb9", "browse_binary")
        assert body["binary"] is True
        assert "lines" not in body  # the live binary shape omits lines entirely
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", return_value=_response(body)):
            result = fetcher.browse("PROJ", "my-repo", path="logo.png")
        assert result.kind == "FILE"
        assert result.binary is True
        assert not result.lines
