"""Unit tests for the Bitbucket Data Center Pydantic models."""

import logging

from mcp_atlassian.models.bitbucket import (
    BitbucketActivity,
    BitbucketComment,
    BitbucketFileDiff,
    BitbucketProject,
    BitbucketPullRequest,
    BitbucketPullRequestDiff,
    BitbucketRepository,
)

# Shapes mirror the pinned Bitbucket DC REST spec (RestProject / RestRepository).
_PROJECT_API = {
    "key": "PROJ",
    "id": 11,
    "name": "My Project",
    "description": "A project",
    "type": "NORMAL",
    "public": False,
    "links": {"self": [{"href": "https://bitbucket.example.com/projects/PROJ"}]},
}

_REPO_API = {
    "slug": "my-repo",
    "id": 42,
    "name": "My Repo",
    "description": "The repo",
    "scmId": "git",
    "state": "AVAILABLE",
    "statusMessage": "Available",
    "forkable": True,
    "archived": False,
    "public": False,
    "defaultBranch": "refs/heads/main",
    "project": _PROJECT_API,
    "links": {"clone": [{"href": "ssh://git@bitbucket/PROJ/my-repo.git"}]},
}


class TestBitbucketProject:
    """BitbucketProject parsing and simplification."""

    def test_from_api_response_maps_fields(self):
        project = BitbucketProject.from_api_response(_PROJECT_API)
        assert project.key == "PROJ"
        assert project.id == "11"  # stringified
        assert project.name == "My Project"
        assert project.description == "A project"
        assert project.type == "NORMAL"
        assert project.public is False

    def test_from_api_response_empty_returns_default(self):
        project = BitbucketProject.from_api_response({})
        assert project.key == ""
        assert project.id == ""

    def test_from_api_response_non_dict_returns_default(self):
        project = BitbucketProject.from_api_response("nonsense")  # type: ignore[arg-type]
        assert project.key == ""

    def test_explicit_null_fields_fall_back_to_sentinels(self):
        """A present-but-null key/name uses the sentinel, not the string 'None'."""
        project = BitbucketProject.from_api_response({"key": None, "name": None})
        assert project.key == ""
        assert project.name == "Unknown"

    def test_to_simplified_dict_includes_present_fields(self):
        result = BitbucketProject.from_api_response(_PROJECT_API).to_simplified_dict()
        assert result == {
            "key": "PROJ",
            "name": "My Project",
            "description": "A project",
            "type": "NORMAL",
            "public": False,
        }

    def test_to_simplified_dict_omits_absent_optionals(self):
        result = BitbucketProject.from_api_response(
            {"key": "P", "name": "P"}
        ).to_simplified_dict()
        assert result == {"key": "P", "name": "P"}


class TestBitbucketRepository:
    """BitbucketRepository parsing and simplification."""

    def test_from_api_response_maps_fields_and_nested_project(self):
        repo = BitbucketRepository.from_api_response(_REPO_API)
        assert repo.slug == "my-repo"
        assert repo.id == "42"  # stringified
        assert repo.name == "My Repo"
        assert repo.description == "The repo"
        assert repo.scm_id == "git"  # from scmId
        assert repo.state == "AVAILABLE"
        assert repo.forkable is True
        assert repo.archived is False
        assert repo.public is False
        assert repo.default_branch == "refs/heads/main"  # from defaultBranch
        assert repo.project is not None
        assert repo.project.key == "PROJ"

    def test_from_api_response_without_project(self):
        data = {k: v for k, v in _REPO_API.items() if k != "project"}
        repo = BitbucketRepository.from_api_response(data)
        assert repo.project is None

    def test_malformed_project_degrades_to_none_not_raise(self):
        data = {**_REPO_API, "project": "not-an-object"}
        repo = BitbucketRepository.from_api_response(data)
        assert repo.project is None

    def test_from_api_response_empty_returns_default(self):
        repo = BitbucketRepository.from_api_response({})
        assert repo.slug == ""
        assert repo.project is None

    def test_explicit_null_fields_fall_back_to_sentinels(self):
        """A present-but-null slug/name uses the sentinel, not the string 'None'."""
        repo = BitbucketRepository.from_api_response({"slug": None, "name": None})
        assert repo.slug == ""
        assert repo.name == "Unknown"

    def test_to_simplified_dict_full(self):
        result = BitbucketRepository.from_api_response(_REPO_API).to_simplified_dict()
        assert result == {
            "slug": "my-repo",
            "name": "My Repo",
            "description": "The repo",
            "state": "AVAILABLE",
            "default_branch": "refs/heads/main",
            "scm_id": "git",
            "public": False,
            "archived": False,
            "forkable": True,
            "project": {
                "key": "PROJ",
                "name": "My Project",
                "description": "A project",
                "type": "NORMAL",
                "public": False,
            },
        }

    def test_to_simplified_dict_minimal(self):
        result = BitbucketRepository.from_api_response(
            {"slug": "r", "name": "r"}
        ).to_simplified_dict()
        assert result == {"slug": "r", "name": "r"}


# Shapes mirror the pinned Bitbucket DC REST spec (RestPullRequest).
_PR_API = {
    "id": 42,
    "version": 3,
    "title": "Add feature X",
    "description": "Implements X",
    "state": "OPEN",
    "open": True,
    "closed": False,
    "draft": False,
    "locked": False,
    "createdDate": 19990759200,
    "updatedDate": 19990759300,
    "fromRef": {
        "id": "refs/heads/feature-x",
        "displayId": "feature-x",
        "latestCommit": "abc123",
    },
    "toRef": {
        "id": "refs/heads/main",
        "displayId": "main",
        "latestCommit": "def456",
    },
    "author": {
        "user": {"name": "auth1", "slug": "auth1", "displayName": "Author One"},
        "role": "AUTHOR",
        "status": "UNAPPROVED",
        "approved": False,
    },
    "reviewers": [
        {
            "user": {"name": "rev1", "slug": "rev1", "displayName": "Reviewer One"},
            "role": "REVIEWER",
            "status": "APPROVED",
            "approved": True,
        }
    ],
    "participants": [
        {
            "user": {"name": "rev1", "slug": "rev1", "displayName": "Reviewer One"},
            "role": "REVIEWER",
            "status": "APPROVED",
            "approved": True,
        }
    ],
    "links": {"self": [{"href": "https://bitbucket.example.com/pr/42"}]},
}


class TestBitbucketPullRequest:
    """BitbucketPullRequest parsing, author derivation, and simplification."""

    def test_from_api_response_maps_fields(self):
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        assert pr.id == 42
        assert pr.title == "Add feature X"
        assert pr.state == "OPEN"
        assert pr.draft is False
        assert pr.version == 3
        assert pr.created_date == 19990759200
        assert pr.from_ref is not None
        assert pr.from_ref.display_id == "feature-x"
        assert pr.to_ref is not None
        assert pr.to_ref.display_id == "main"

    def test_author_read_from_top_level_field(self):
        """The runtime top-level author is surfaced even when participants omit it."""
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        assert all(p.role != "AUTHOR" for p in pr.participants)
        assert pr.author is not None
        assert pr.author.role == "AUTHOR"
        assert pr.author.user is not None
        assert pr.author.user.name == "auth1"

    def test_author_falls_back_to_author_participant(self):
        """With no top-level author, the AUTHOR-role participant is used."""
        data = {k: v for k, v in _PR_API.items() if k != "author"}
        data["participants"] = [
            {
                "user": {"name": "auth1", "displayName": "Author One"},
                "role": "AUTHOR",
                "status": "UNAPPROVED",
                "approved": False,
            }
        ]
        pr = BitbucketPullRequest.from_api_response(data)
        assert pr.author is not None
        assert pr.author.role == "AUTHOR"
        assert pr.author.user is not None
        assert pr.author.user.name == "auth1"

    def test_empty_author_object_falls_back_to_participant(self):
        """An empty ``author: {}`` counts as absent, so the fallback fires."""
        data = {k: v for k, v in _PR_API.items() if k != "author"}
        data["author"] = {}
        data["participants"] = [
            {
                "user": {"name": "auth1", "displayName": "Author One"},
                "role": "AUTHOR",
                "status": "UNAPPROVED",
                "approved": False,
            }
        ]
        pr = BitbucketPullRequest.from_api_response(data)
        assert pr.author is not None
        assert pr.author.role == "AUTHOR"
        assert pr.author.user is not None
        assert pr.author.user.name == "auth1"

    def test_reviewer_status_preserved(self):
        pr = BitbucketPullRequest.from_api_response(_PR_API)
        assert len(pr.reviewers) == 1
        assert pr.reviewers[0].status == "APPROVED"
        assert pr.reviewers[0].approved is True

    def test_no_author_anywhere_leaves_author_none(self):
        data = {k: v for k, v in _PR_API.items() if k != "author"}
        data["participants"] = []
        pr = BitbucketPullRequest.from_api_response(data)
        assert pr.author is None

    def test_from_api_response_empty_returns_default(self):
        pr = BitbucketPullRequest.from_api_response({})
        assert pr.id == 0
        assert pr.title == "Unknown"
        assert pr.reviewers == []

    def test_to_simplified_dict_shape(self):
        result = BitbucketPullRequest.from_api_response(_PR_API).to_simplified_dict()
        assert result["id"] == 42
        assert result["title"] == "Add feature X"
        assert result["state"] == "OPEN"
        assert result["from_ref"] == {
            "display_id": "feature-x",
            "id": "refs/heads/feature-x",
            "latest_commit": "abc123",
        }
        assert result["author"]["user"] == {
            "name": "auth1",
            "display_name": "Author One",
        }
        assert result["reviewers"][0]["status"] == "APPROVED"
        # The write-only links object is never surfaced.
        assert "links" not in result


# The /diff endpoint body is a RestDiffResponse wrapping RestDiff objects in a
# `diffs` array.
_DIFF_API = {
    "fromHash": "abc123",
    "toHash": "def456",
    "diffs": [
        {
            "source": None,
            "destination": {"components": ["src", "app.py"], "name": "app.py"},
            "binary": False,
            "truncated": False,
            "hunks": [
                {
                    "context": "def foo():",
                    "sourceLine": 1,
                    "sourceSpan": 2,
                    "destinationLine": 1,
                    "destinationSpan": 3,
                    "truncated": False,
                    "segments": [
                        {
                            "type": "CONTEXT",
                            "truncated": False,
                            "lines": [
                                {"source": 1, "destination": 1, "line": "import os"}
                            ],
                        },
                        {
                            "type": "ADDED",
                            "truncated": False,
                            "lines": [
                                {"source": 2, "destination": 2, "line": "import sys"},
                                {"source": 2, "destination": 3, "line": "import json"},
                            ],
                        },
                    ],
                }
            ],
        }
    ],
    "truncated": False,
}


class TestBitbucketPullRequestDiff:
    """Structured diff parsing, path joining, and line-budget truncation."""

    def test_parses_files_and_paths(self):
        diff = BitbucketPullRequestDiff.from_api_response(_DIFF_API)
        assert diff.total_files == 1
        assert len(diff.files) == 1
        file = diff.files[0]
        # Added file: no source, destination joined from components.
        assert file.source_path is None
        assert file.destination_path == "src/app.py"
        assert file.binary is False
        assert len(file.hunks) == 1
        assert file.hunks[0].segments[1].type == "ADDED"
        assert file.hunks[0].segments[1].lines[0].line == "import sys"

    def test_not_truncated_under_budget(self):
        diff = BitbucketPullRequestDiff.from_api_response(
            _DIFF_API, max_lines_per_file=100
        )
        assert diff.truncated is False
        assert diff.files[0].line_truncated is False
        assert diff.files[0].omitted_lines == 0

    def test_truncates_at_max_lines_per_file(self):
        """A 2-line cap keeps 2 of the 3 lines and flags truncation + omitted."""
        diff = BitbucketPullRequestDiff.from_api_response(
            _DIFF_API, max_lines_per_file=2
        )
        file = diff.files[0]
        kept = sum(len(seg.lines) for hunk in file.hunks for seg in hunk.segments)
        assert kept == 2
        assert file.line_truncated is True
        assert file.omitted_lines == 1
        assert diff.truncated is True

    def test_server_truncated_propagates(self):
        data = {"diffs": [{"destination": {"name": "f"}, "truncated": True}]}
        diff = BitbucketPullRequestDiff.from_api_response(data)
        assert diff.files[0].server_truncated is True
        assert diff.truncated is True

    def test_max_files_caps_file_list(self):
        data = {"diffs": [{"destination": {"name": f"f{i}"}} for i in range(5)]}
        diff = BitbucketPullRequestDiff.from_api_response(data, max_files=2)
        assert diff.total_files == 5
        assert len(diff.files) == 2
        assert diff.truncated is True

    def test_empty_diffs_yields_empty_diff_no_warning(self, caplog):
        """A legitimate empty `diffs: []` is a no-change diff, logged silently."""
        with caplog.at_level(logging.WARNING):
            diff = BitbucketPullRequestDiff.from_api_response(
                {"fromHash": "a", "toHash": "a", "diffs": []}
            )
        assert diff.files == []
        assert diff.total_files == 0
        assert diff.truncated is False
        assert caplog.records == []

    def test_missing_diffs_key_warns_and_yields_empty_diff(self, caplog):
        """A non-empty body with no `diffs` key warns and degrades to empty."""
        with caplog.at_level(logging.WARNING):
            diff = BitbucketPullRequestDiff.from_api_response({"size": 0})
        assert diff.files == []
        assert diff.total_files == 0
        assert diff.truncated is False
        assert any("diffs" in r.message for r in caplog.records)

    def test_null_diffs_warns_and_yields_empty_diff(self, caplog):
        """A present-but-null `diffs` is shape drift too: warn, degrade to empty."""
        with caplog.at_level(logging.WARNING):
            diff = BitbucketPullRequestDiff.from_api_response(
                {"fromHash": "a", "toHash": "b", "diffs": None}
            )
        assert diff.files == []
        assert diff.total_files == 0
        assert any("diffs" in r.message for r in caplog.records)

    def test_response_level_truncated_propagates(self):
        """The response-level `truncated` flag forces the diff truncated."""
        diff = BitbucketPullRequestDiff.from_api_response(
            {"diffs": [{"destination": {"name": "f"}}], "truncated": True}
        )
        assert diff.files[0].server_truncated is False
        assert diff.truncated is True

    def test_non_dict_body_returns_empty(self):
        diff = BitbucketPullRequestDiff.from_api_response("nope")  # type: ignore[arg-type]
        assert diff.files == []

    def test_single_file_to_simplified_dict(self):
        file = BitbucketFileDiff.from_api_response(_DIFF_API["diffs"][0])
        result = file.to_simplified_dict()
        assert result["destination_path"] == "src/app.py"
        assert result["hunks"][0]["segments"][1]["type"] == "ADDED"

    def test_binary_file_is_retained(self):
        """A binary file (no hunks) is kept with its binary flag round-tripped."""
        data = {"diffs": [{"destination": {"name": "img.png"}, "binary": True}]}
        diff = BitbucketPullRequestDiff.from_api_response(data)
        assert len(diff.files) == 1
        file = diff.files[0]
        assert file.binary is True
        assert file.hunks == []
        assert file.to_simplified_dict()["binary"] is True

    def test_combined_file_and_line_caps_both_truncate(self):
        """truncated is true when either the file cap or a line budget trims."""
        data = {
            "diffs": [
                {
                    "destination": {"name": f"f{i}"},
                    "hunks": [
                        {
                            "segments": [
                                {
                                    "type": "ADDED",
                                    "lines": [
                                        {"line": "a"},
                                        {"line": "b"},
                                        {"line": "c"},
                                    ],
                                }
                            ]
                        }
                    ],
                }
                for i in range(3)
            ]
        }
        diff = BitbucketPullRequestDiff.from_api_response(
            data, max_files=2, max_lines_per_file=2
        )
        assert diff.total_files == 3
        assert len(diff.files) == 2
        # Each returned file is line-truncated AND files were dropped.
        assert all(f.line_truncated for f in diff.files)
        assert diff.truncated is True

    def test_whole_hunk_dropped_at_budget_boundary(self):
        """When the budget is spent by hunk 1, hunk 2 is dropped entirely."""
        data = {
            "diffs": [
                {
                    "destination": {"name": "f.py"},
                    "hunks": [
                        {
                            "segments": [
                                {
                                    "type": "ADDED",
                                    "lines": [{"line": "1"}, {"line": "2"}],
                                }
                            ]
                        },
                        {
                            "segments": [
                                {
                                    "type": "ADDED",
                                    "lines": [
                                        {"line": "3"},
                                        {"line": "4"},
                                        {"line": "5"},
                                    ],
                                }
                            ]
                        },
                    ],
                }
            ]
        }
        diff = BitbucketPullRequestDiff.from_api_response(data, max_lines_per_file=2)
        file = diff.files[0]
        # Hunk 1 fully consumes the budget; hunk 2 is omitted, not emitted empty.
        assert len(file.hunks) == 1
        kept = sum(len(seg.lines) for hunk in file.hunks for seg in hunk.segments)
        assert kept == 2
        assert file.omitted_lines == 3
        assert file.line_truncated is True
        assert diff.truncated is True


# Shapes mirror the pinned spec (RestPullRequestActivity + RestComment).
_ACTIVITY_COMMENTED = {
    "id": 101,
    "action": "COMMENTED",
    "createdDate": 19990759200,
    "user": {"name": "rev1", "slug": "rev1", "displayName": "Reviewer One"},
    "comment": {
        "id": 9,
        "text": "Looks good",
        "author": {"name": "rev1", "displayName": "Reviewer One"},
        "createdDate": 19990759200,
        "state": "OPEN",
        "severity": "NORMAL",
        "comments": [{"id": 10, "text": "thanks"}],
    },
}
_ACTIVITY_APPROVED = {
    "id": 102,
    "action": "APPROVED",
    "createdDate": 19990759300,
    "user": {"name": "rev1", "displayName": "Reviewer One"},
}


class TestBitbucketActivity:
    """Activity parsing and the COMMENTED comment passthrough."""

    def test_commented_activity_surfaces_comment(self):
        activity = BitbucketActivity.from_api_response(_ACTIVITY_COMMENTED)
        assert activity.action == "COMMENTED"
        assert activity.comment is not None
        assert activity.comment.text == "Looks good"
        assert activity.comment.author is not None
        assert activity.comment.author.name == "rev1"
        # Nested replies are counted, not recursed.
        assert activity.comment.reply_count == 1

    def test_non_comment_activity_has_no_comment(self):
        activity = BitbucketActivity.from_api_response(_ACTIVITY_APPROVED)
        assert activity.action == "APPROVED"
        assert activity.comment is None

    def test_to_simplified_dict_includes_comment(self):
        result = BitbucketActivity.from_api_response(
            _ACTIVITY_COMMENTED
        ).to_simplified_dict()
        assert result["action"] == "COMMENTED"
        assert result["user"] == {"name": "rev1", "display_name": "Reviewer One"}
        assert result["comment"]["text"] == "Looks good"
        assert result["comment"]["severity"] == "NORMAL"

    def test_empty_activity_returns_default(self):
        activity = BitbucketActivity.from_api_response({})
        assert activity.id == 0
        assert activity.comment is None


class TestBitbucketComment:
    """RestComment parsing, including the version optimistic-lock token."""

    def test_parses_version_and_surfaces_it(self):
        """version is read and surfaced (needed for a later edit/delete)."""
        comment = BitbucketComment.from_api_response(
            {"id": 9, "version": 3, "text": "note", "severity": "BLOCKER"}
        )
        assert comment.id == 9
        assert comment.version == 3
        result = comment.to_simplified_dict()
        assert result["version"] == 3
        assert result["severity"] == "BLOCKER"

    def test_version_zero_is_surfaced_not_dropped(self):
        """A freshly created comment has version 0, which must not be dropped."""
        comment = BitbucketComment.from_api_response({"id": 9, "version": 0})
        assert comment.version == 0
        assert comment.to_simplified_dict()["version"] == 0

    def test_missing_version_is_omitted(self):
        """A comment without a version omits the key rather than emitting null."""
        comment = BitbucketComment.from_api_response({"id": 9, "text": "x"})
        assert comment.version is None
        assert "version" not in comment.to_simplified_dict()
