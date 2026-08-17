"""Unit tests for the Bitbucket Data Center Pydantic models."""

import logging

import pytest

from mcp_atlassian.models.bitbucket import (
    BitbucketActivity,
    BitbucketBranch,
    BitbucketChange,
    BitbucketComment,
    BitbucketCommit,
    BitbucketDiffHunk,
    BitbucketDiffSegment,
    BitbucketDirectoryEntry,
    BitbucketFileDiff,
    BitbucketProject,
    BitbucketPullRequest,
    BitbucketPullRequestDiff,
    BitbucketRepository,
    BitbucketTag,
    BitbucketUser,
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


class TestBitbucketUser:
    """BitbucketUser: identity fields only, with the name/slug fallback."""

    def test_drops_contact_and_link_fields(self):
        user = BitbucketUser.from_api_response(
            {
                "name": "jdoe",
                "slug": "jdoe",
                "displayName": "J. Doe",
                "emailAddress": "jdoe@example.com",
                "avatarUrl": "https://bitbucket.example.com/avatar",
                "links": {"self": [{"href": "https://bitbucket.example.com/u"}]},
            }
        )
        assert user.to_simplified_dict() == {"name": "jdoe", "display_name": "J. Doe"}

    def test_falls_back_to_slug_when_name_is_absent(self):
        user = BitbucketUser.from_api_response({"slug": "jdoe-slug"})
        assert user.name == "jdoe-slug"

    def test_null_name_falls_back_to_slug(self):
        user = BitbucketUser.from_api_response({"name": None, "slug": "jdoe-slug"})
        assert user.name == "jdoe-slug"

    def test_unknown_display_name_is_omitted(self):
        user = BitbucketUser.from_api_response({"name": "jdoe"})
        assert user.display_name == "Unknown"
        assert user.to_simplified_dict() == {"name": "jdoe"}

    def test_empty_and_non_dict_input_default(self):
        assert BitbucketUser.from_api_response({}).name == ""
        assert BitbucketUser.from_api_response(None).name == ""  # type: ignore[arg-type]


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
        """A present-but-null key/name falls back to the sentinel (no str(None))."""
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
        """A present-but-null slug/name falls back to the sentinel (no str(None))."""
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


# Shapes mirror the pinned Bitbucket DC REST spec (RestBranch / RestTag).
_BRANCH_API = {
    "id": "refs/heads/main",
    "displayId": "main",
    "latestCommit": "abc123",
    "latestChangeset": "abc123",  # legacy alias the model must drop
    "type": "BRANCH",
    "isDefault": True,  # the live wire key (spec documents `default`)
}

_TAG_API = {
    "id": "refs/tags/v1.0.0",
    "displayId": "v1.0.0",
    "latestCommit": "def456",
    "latestChangeset": "def456",  # legacy alias the model must drop
    "type": "TAG",
    "hash": "objsha789",
}


class TestBitbucketBranch:
    """BitbucketBranch parsing and simplification."""

    def test_from_api_response_maps_fields(self):
        branch = BitbucketBranch.from_api_response(_BRANCH_API)
        assert branch.id == "refs/heads/main"
        assert branch.display_id == "main"  # from displayId
        assert branch.latest_commit == "abc123"  # from latestCommit
        assert branch.type == "BRANCH"
        assert branch.is_default is True  # from default

    def test_from_api_response_empty_returns_default(self):
        branch = BitbucketBranch.from_api_response({})
        assert branch.id == ""
        assert branch.display_id == ""
        assert branch.latest_commit is None
        assert branch.is_default is None

    def test_from_api_response_non_dict_returns_default(self):
        branch = BitbucketBranch.from_api_response("nonsense")  # type: ignore[arg-type]
        assert branch.id == ""

    def test_minimal_ref_populates_identity_only(self):
        """A RestMinimalRef (no commit/default) leaves those fields unset."""
        branch = BitbucketBranch.from_api_response(
            {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH"}
        )
        assert branch.display_id == "main"
        assert branch.latest_commit is None
        assert branch.is_default is None

    def test_to_simplified_dict_omits_absent_optionals(self):
        result = BitbucketBranch.from_api_response(
            {"id": "refs/heads/x", "displayId": "x"}
        ).to_simplified_dict()
        assert result == {"display_id": "x", "id": "refs/heads/x"}
        assert "latest_commit" not in result
        assert "is_default" not in result

    def test_to_simplified_dict_never_surfaces_latest_changeset(self):
        result = BitbucketBranch.from_api_response(_BRANCH_API).to_simplified_dict()
        assert result == {
            "display_id": "main",
            "id": "refs/heads/main",
            "latest_commit": "abc123",
            "type": "BRANCH",
            "is_default": True,
        }
        assert "latestChangeset" not in result
        assert "latest_changeset" not in result

    def test_to_summary_dict_is_triage_only(self):
        result = BitbucketBranch.from_api_response(_BRANCH_API).to_summary_dict()
        assert result == {"display_id": "main", "latest_commit": "abc123"}

    def test_to_summary_dict_omits_absent_latest_commit(self):
        """A minimal ref (no commit) summarises to display_id only, by design."""
        result = BitbucketBranch.from_api_response(
            {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH"}
        ).to_summary_dict()
        assert result == {"display_id": "main"}
        assert "latest_commit" not in result


class TestBitbucketTag:
    """BitbucketTag parsing and simplification."""

    def test_from_api_response_maps_fields(self):
        tag = BitbucketTag.from_api_response(_TAG_API)
        assert tag.id == "refs/tags/v1.0.0"
        assert tag.display_id == "v1.0.0"
        assert tag.latest_commit == "def456"
        assert tag.type == "TAG"
        assert tag.hash == "objsha789"  # annotated-tag object SHA

    def test_lightweight_tag_has_null_hash(self):
        data = {k: v for k, v in _TAG_API.items() if k != "hash"}
        tag = BitbucketTag.from_api_response(data)
        assert tag.hash is None

    def test_from_api_response_empty_returns_default(self):
        tag = BitbucketTag.from_api_response({})
        assert tag.id == ""
        assert tag.hash is None

    def test_from_api_response_non_dict_returns_default(self):
        tag = BitbucketTag.from_api_response("nonsense")  # type: ignore[arg-type]
        assert tag.id == ""

    def test_to_simplified_dict_omits_absent_optionals(self):
        result = BitbucketTag.from_api_response(
            {"id": "refs/tags/x", "displayId": "x"}
        ).to_simplified_dict()
        assert result == {"display_id": "x", "id": "refs/tags/x"}
        assert "hash" not in result
        assert "latest_commit" not in result

    def test_to_simplified_dict_never_surfaces_latest_changeset(self):
        result = BitbucketTag.from_api_response(_TAG_API).to_simplified_dict()
        assert result == {
            "display_id": "v1.0.0",
            "id": "refs/tags/v1.0.0",
            "latest_commit": "def456",
            "type": "TAG",
            "hash": "objsha789",
        }
        assert "latestChangeset" not in result
        assert "latest_changeset" not in result

    def test_to_summary_dict_is_triage_only(self):
        result = BitbucketTag.from_api_response(_TAG_API).to_summary_dict()
        assert result == {"display_id": "v1.0.0", "latest_commit": "def456"}


# Shapes mirror the pinned Bitbucket DC REST spec (RestCommit). author/committer
# are inline {name, emailAddress} objects (inline persons, with no slug or account).
_COMMIT_API = {
    "id": "abc123def456abc123def456abc123def456abcd",
    "displayId": "abc123d",
    "message": "Add feature X\n\nMore detail in the body.",
    "author": {"name": "Jane Dev", "emailAddress": "jane@corp.example.com"},
    "authorTimestamp": 1700000000000,
    "committer": {"name": "Repo Bot", "emailAddress": "bot@corp.example.com"},
    "committerTimestamp": 1700000100000,
    "parents": [
        {"id": "parent1sha", "displayId": "parent1"},
        {"id": "parent2sha", "displayId": "parent2"},
    ],
}


class TestBitbucketCommit:
    """BitbucketCommit parsing and simplification."""

    def test_from_api_response_maps_fields(self):
        commit = BitbucketCommit.from_api_response(_COMMIT_API)
        assert commit.id == "abc123def456abc123def456abc123def456abcd"
        assert commit.display_id == "abc123d"
        assert commit.message.startswith("Add feature X")
        assert commit.author_timestamp == 1700000000000
        assert commit.committer_timestamp == 1700000100000

    def test_inline_author_parses_to_user_with_email_dropped(self):
        """author/committer parse to BitbucketUser; emailAddress is dropped."""
        commit = BitbucketCommit.from_api_response(_COMMIT_API)
        assert isinstance(commit.author, BitbucketUser)
        assert commit.author.name == "Jane Dev"
        assert isinstance(commit.committer, BitbucketUser)
        assert commit.committer.name == "Repo Bot"
        # PII minimisation: the inline email is dropped at the model boundary.
        simplified = commit.to_simplified_dict()
        assert "jane@corp.example.com" not in str(simplified)
        assert "emailAddress" not in str(simplified)

    def test_parents_extracted_as_ids(self):
        commit = BitbucketCommit.from_api_response(_COMMIT_API)
        assert commit.parents == ["parent1sha", "parent2sha"]

    def test_malformed_parents_entries_skipped(self):
        """Non-dict / id-less parent entries are skipped."""
        data = {**_COMMIT_API, "parents": [{"id": "ok"}, {"displayId": "x"}, "junk"]}
        commit = BitbucketCommit.from_api_response(data)
        assert commit.parents == ["ok"]

    def test_from_api_response_empty_returns_default(self):
        commit = BitbucketCommit.from_api_response({})
        assert commit.id == ""
        assert commit.display_id == ""
        assert commit.message == ""
        assert commit.parents == []
        assert commit.author is None
        assert commit.committer is None
        assert commit.author_timestamp is None

    def test_from_api_response_non_dict_returns_default(self):
        commit = BitbucketCommit.from_api_response("nonsense")  # type: ignore[arg-type]
        assert commit.id == ""

    def test_to_simplified_dict_omits_absent_optionals(self):
        commit = BitbucketCommit.from_api_response(
            {"id": "sha", "displayId": "sha7", "message": "msg"}
        )
        result = commit.to_simplified_dict()
        assert result == {"id": "sha", "display_id": "sha7", "message": "msg"}
        assert "author" not in result
        assert "committer" not in result
        assert "author_timestamp" not in result
        assert "parents" not in result

    def test_to_simplified_dict_full(self):
        result = BitbucketCommit.from_api_response(_COMMIT_API).to_simplified_dict()
        assert result["id"] == "abc123def456abc123def456abc123def456abcd"
        assert result["display_id"] == "abc123d"
        assert result["author"] == {"name": "Jane Dev"}
        assert result["committer"] == {"name": "Repo Bot"}
        assert result["author_timestamp"] == 1700000000000
        assert result["committer_timestamp"] == 1700000100000
        assert result["parents"] == ["parent1sha", "parent2sha"]

    def test_to_summary_dict_is_triage_only(self):
        result = BitbucketCommit.from_api_response(_COMMIT_API).to_summary_dict()
        assert result == {
            "display_id": "abc123d",
            "author": "Jane Dev",
            "author_timestamp": 1700000000000,
            # only the first line of a multi-line message
            "message": "Add feature X",
        }


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
        # The write-only links object is not surfaced.
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

    def test_missing_diffs_key_raises(self):
        """A non-empty body with no `diffs` key is a response-shape error, not
        a valid-looking empty ("no change") diff."""
        with pytest.raises(ValueError, match="'diffs' is absent or not a list"):
            BitbucketPullRequestDiff.from_api_response({"size": 0})

    def test_null_diffs_raises(self):
        """A present-but-null `diffs` is shape drift too and raises."""
        with pytest.raises(ValueError, match="'diffs' is absent or not a list"):
            BitbucketPullRequestDiff.from_api_response(
                {"fromHash": "a", "toHash": "b", "diffs": None}
            )

    def test_non_object_diff_entry_raises(self):
        """A non-object `diffs` entry is a response-shape error rather than an
        empty, untruncated diff."""
        with pytest.raises(ValueError, match="every 'diffs' entry to be an object"):
            BitbucketPullRequestDiff.from_api_response({"diffs": ["bad"]})

    @pytest.mark.parametrize(
        ("label", "file", "match"),
        [
            ("hunks_string", {"hunks": "garbage"}, "'hunks' is str, not a list"),
            ("hunk_string", {"hunks": ["x"]}, "every 'hunks' entry to be an object"),
            (
                "segments_string",
                {"hunks": [{"segments": "garbage"}]},
                "'segments' is str, not a list",
            ),
            (
                "segment_string",
                {"hunks": [{"segments": ["x"]}]},
                "every 'segments' entry to be an object",
            ),
            (
                "lines_string",
                {"hunks": [{"segments": [{"type": "ADDED", "lines": "x"}]}]},
                "'lines' is str, not a list",
            ),
            (
                "line_scalar",
                {"hunks": [{"segments": [{"type": "ADDED", "lines": ["x", 5]}]}]},
                "every 'lines' entry to be an object",
            ),
            (
                "line_text_object",
                {"hunks": [{"segments": [{"lines": [{"line": {"a": 1}}]}]}]},
                "'line' in BitbucketDiffLine is dict, not a string",
            ),
        ],
    )
    def test_malformed_nested_entry_raises(self, label, file, match):
        """A malformed nested collection or line is a response-shape error."""
        body = {"diffs": [{"destination": {"toString": "a.py"}, **file}]}
        with pytest.raises(ValueError, match=match):
            BitbucketPullRequestDiff.from_api_response(body)

    def test_malformed_hunk_past_the_budget_raises(self):
        """A hunk skipped by the line budget is still shape-checked."""
        body = {
            "diffs": [
                {
                    "hunks": [
                        {"segments": [{"lines": [{"line": "1"}, {"line": "2"}]}]},
                        {"segments": [{"lines": "garbage"}]},
                    ]
                }
            ]
        }
        with pytest.raises(ValueError, match="'lines' is str, not a list"):
            BitbucketPullRequestDiff.from_api_response(body, max_lines_per_file=2)

    @pytest.mark.parametrize(
        ("label", "file"),
        [
            ("hunks_null", {"hunks": None}),
            ("segments_null", {"hunks": [{"segments": None}]}),
            ("lines_null", {"hunks": [{"segments": [{"lines": None}]}]}),
        ],
    )
    def test_null_nested_collection_is_empty(self, label, file):
        """A null collection is treated like an absent one."""
        body = {"diffs": [{"destination": {"toString": "a.py"}, **file}]}
        diff = BitbucketPullRequestDiff.from_api_response(body)
        assert diff.truncated is False
        assert all(
            not seg.lines for hunk in diff.files[0].hunks for seg in hunk.segments
        )

    def test_absent_nested_collections_are_empty(self):
        """A hunk without segments keeps its coordinates as an empty hunk."""
        body = {"diffs": [{"hunks": [{"sourceLine": 3, "destinationLine": 4}]}]}
        diff = BitbucketPullRequestDiff.from_api_response(body)
        assert diff.truncated is False
        hunk = diff.files[0].hunks[0]
        assert hunk.segments == []
        assert hunk.to_simplified_dict() == {
            "source_line": 3,
            "destination_line": 4,
            "segments": [],
        }

    def test_hunk_and_segment_constructors_guard_input(self):
        """The public hunk and segment constructors default on empty input."""
        assert BitbucketDiffHunk.from_api_response({}).segments == []
        assert BitbucketDiffHunk.from_api_response("x").segments == []  # type: ignore[arg-type]
        assert BitbucketDiffSegment.from_api_response({}).lines == []
        hunk = BitbucketDiffHunk.from_api_response(_DIFF_API["diffs"][0]["hunks"][0])
        assert hunk.segments[1].lines[0].line == "import sys"
        with pytest.raises(ValueError, match="'segments' is int, not a list"):
            BitbucketDiffHunk.from_api_response({"segments": 1})
        with pytest.raises(ValueError, match="every 'lines' entry to be an object"):
            BitbucketDiffSegment.from_api_response({"lines": [1]})

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
        # Hunk 1 fully consumes the budget; hunk 2 is omitted entirely.
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
        # Nested replies are counted without recursion.
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

    def test_thread_resolved_true_is_parsed_and_surfaced(self):
        """threadResolved=True is parsed and surfaced to confirm a resolve."""
        comment = BitbucketComment.from_api_response(
            {"id": 9, "version": 4, "threadResolved": True}
        )
        assert comment.thread_resolved is True
        assert comment.to_simplified_dict()["thread_resolved"] is True

    def test_thread_resolved_false_is_surfaced_not_dropped(self):
        """threadResolved=False (an open thread) must not be dropped as falsy."""
        comment = BitbucketComment.from_api_response(
            {"id": 9, "version": 4, "threadResolved": False}
        )
        assert comment.thread_resolved is False
        assert comment.to_simplified_dict()["thread_resolved"] is False

    def test_missing_thread_resolved_is_omitted(self):
        """A comment without threadResolved omits the key.

        The field is not present on every RestComment response, so an absent
        value parses to None and is omitted rather than emitting a misleading
        false.
        """
        comment = BitbucketComment.from_api_response({"id": 9, "version": 4})
        assert comment.thread_resolved is None
        assert "thread_resolved" not in comment.to_simplified_dict()


# Wire-shape guards. The shapes below are synthetic but mirror the *structure*
# of Bitbucket DC 9.x runtime responses. They lock the
# observed wire shape so a future version bump that drifts it fails loudly here
# instead of degrading silently in production.
_V9_DIFF_ENVELOPE = {
    # 9.x returns the minimal RestDiffResponse with no `values`/pagination keys.
    "diffs": _DIFF_API["diffs"],
    "truncated": False,
}


def _pr_with_top_level_author(name: str, status: str) -> dict:
    """A PR body carrying a top-level author and a non-author participant only.

    The author is absent from `participants` here, so a participant-derived
    lookup would drop it; the value must come from the top-level `author`.
    """
    return {
        **{k: v for k, v in _PR_API.items() if k != "author"},
        "author": {
            "user": {"name": name, "slug": name, "displayName": name.title()},
            "role": "AUTHOR",
            "status": status,
            "approved": status == "APPROVED",
        },
        "participants": [
            {
                "user": {"name": "part1", "slug": "part1"},
                "role": "PARTICIPANT",
                "status": "UNAPPROVED",
                "approved": False,
            }
        ],
    }


class TestBitbucketDirectoryEntry:
    """The browse directory child model."""

    def test_from_api_response_joins_components(self):
        entry = BitbucketDirectoryEntry.from_api_response(
            {
                "path": {"components": ["src", "app.py"], "name": "app.py"},
                "type": "FILE",
                "size": 128,
                "contentId": "blob-abc",
            }
        )
        assert entry.path == "src/app.py"
        assert entry.type == "FILE"
        assert entry.size == 128
        assert entry.content_id == "blob-abc"

    def test_directory_entry_has_no_size_or_content(self):
        entry = BitbucketDirectoryEntry.from_api_response(
            {"path": {"components": ["src"], "name": "src"}, "type": "DIRECTORY"}
        )
        assert entry.path == "src"
        assert entry.type == "DIRECTORY"
        assert entry.size is None
        assert entry.content_id is None

    def test_submodule_entry_type_preserved(self):
        entry = BitbucketDirectoryEntry.from_api_response(
            {
                "path": {"components": ["vendor"], "name": "vendor"},
                "type": "SUBMODULE",
                "size": 0,
            }
        )
        assert entry.type == "SUBMODULE"
        assert "type" in entry.to_simplified_dict()

    def test_from_api_response_empty_returns_default(self):
        entry = BitbucketDirectoryEntry.from_api_response({})
        assert entry.path is None
        assert entry.type is None

    def test_from_api_response_non_dict_returns_default(self):
        entry = BitbucketDirectoryEntry.from_api_response(["nope"])  # type: ignore[arg-type]
        assert entry.path is None

    def test_to_simplified_dict_omits_absent_optionals(self):
        entry = BitbucketDirectoryEntry.from_api_response(
            {"path": {"components": ["x"], "name": "x"}, "type": "DIRECTORY"}
        )
        assert entry.to_simplified_dict() == {"path": "x", "type": "DIRECTORY"}

    def test_to_simplified_dict_full(self):
        entry = BitbucketDirectoryEntry.from_api_response(
            {
                "path": {"components": ["a", "b.txt"], "name": "b.txt"},
                "type": "FILE",
                "size": 5,
                "contentId": "h",
            }
        )
        assert entry.to_simplified_dict() == {
            "path": "a/b.txt",
            "type": "FILE",
            "size": 5,
            "content_id": "h",
        }


class TestBitbucketWireShapes:
    """The read models parse the DC 9.x runtime wire shapes."""

    def test_diff_envelope_reads_diffs(self):
        """9.x carries the minimal `{diffs, truncated}` shape (no `values`)."""
        diff = BitbucketPullRequestDiff.from_api_response(_V9_DIFF_ENVELOPE)
        assert diff.total_files == 1
        assert len(diff.files) == 1
        assert diff.files[0].destination_path == "src/app.py"

    @pytest.mark.parametrize(
        ("label", "data"),
        [
            # Both the `get` and `list` responses carry a top-level author;
            # the absent-author path is covered separately.
            ("get", _pr_with_top_level_author("auth9", "APPROVED")),
            ("list", _pr_with_top_level_author("auth9l", "UNAPPROVED")),
        ],
    )
    def test_top_level_author_surfaced(self, label, data):
        pr = BitbucketPullRequest.from_api_response(data)
        assert all(p.role != "AUTHOR" for p in pr.participants)
        assert pr.author is not None
        assert pr.author.role == "AUTHOR"
        assert pr.author.user is not None
        assert pr.author.user.name == data["author"]["user"]["name"]
        assert pr.to_simplified_dict()["author"]["role"] == "AUTHOR"

    def test_additive_and_unknown_wire_fields_are_ignored(self):
        """The wire carries fields the models don't map (repo `scalable`;
        PR `pullRequestLinks`).

        Models build via explicit field-picks, so additive/unknown wire keys are
        dropped rather than breaking deserialization.
        """
        repo = BitbucketRepository.from_api_response(
            {**_REPO_API, "scalable": True, "futureField": {"nested": 1}}
        )
        assert repo.slug == "my-repo"
        assert repo.default_branch == "refs/heads/main"
        assert "scalable" not in repo.to_simplified_dict()

        pr = BitbucketPullRequest.from_api_response(
            {
                **_PR_API,
                "pullRequestLinks": {"self": [{"href": "https://bb/pr/42"}]},
                "futureField": [1, 2],
            }
        )
        assert pr.id == 42
        assert "pullRequestLinks" not in pr.to_simplified_dict()

    @pytest.mark.parametrize(
        ("label", "data"),
        [
            # The REST spec documents the default-branch flag as `default`;
            # the live 9.x wire emits `isDefault` (exercised by the golden
            # fixtures). The model must read both spellings.
            (
                "spec_default_key",
                {
                    "id": "refs/heads/main",
                    "displayId": "main",
                    "latestCommit": "abc123",
                    "type": "BRANCH",
                    "default": True,
                },
            ),
            (
                "live_isdefault_key",
                {
                    "id": "refs/heads/main",
                    "displayId": "main",
                    "latestCommit": "abc123",
                    "type": "BRANCH",
                    "isDefault": True,
                },
            ),
        ],
    )
    def test_branch_default_flag_spellings(self, label, data):
        branch = BitbucketBranch.from_api_response(data)
        assert branch.display_id == "main"
        assert branch.latest_commit == "abc123"
        assert branch.is_default is True

    def test_tag_parses_wire_shape(self):
        tag = BitbucketTag.from_api_response(
            {
                "id": "refs/tags/v1",
                "displayId": "v1",
                "latestCommit": "def456",
                "hash": "objsha",
                "type": "TAG",
            }
        )
        assert tag.display_id == "v1"
        assert tag.latest_commit == "def456"
        assert tag.hash == "objsha"

    def test_commit_parses_wire_shape(self):
        """The wire carries inline {name, emailAddress} author objects and
        epoch-ms timestamps; the model reads `name`, drops the email, and
        passes the timestamps through unchanged."""
        commit = BitbucketCommit.from_api_response(
            {
                "id": "sha9",
                "displayId": "sha9a",
                "message": "msg9",
                "author": {"name": "Dev9", "emailAddress": "dev9@x"},
                "authorTimestamp": 1700000000000,
                "parents": [{"id": "p9", "displayId": "p9s"}],
            }
        )
        assert commit.display_id == "sha9a"
        assert commit.author is not None
        assert commit.author.name == "Dev9"
        assert commit.author_timestamp == 1700000000000
        assert commit.parents == ["p9"]
        # The inline email is not surfaced.
        assert "emailAddress" not in str(commit.to_simplified_dict())

    def test_commit_additive_and_unknown_wire_fields_are_ignored(self):
        """Additive/unknown commit wire keys are dropped and parsing succeeds."""
        commit = BitbucketCommit.from_api_response(
            {**_COMMIT_API, "properties": {"jira-key": ["X-1"]}, "futureField": True}
        )
        assert commit.display_id == "abc123d"
        assert "properties" not in commit.to_simplified_dict()
        assert "futureField" not in commit.to_simplified_dict()

    def test_ref_additive_and_unknown_wire_fields_are_ignored(self):
        """Additive/unknown ref wire keys are dropped and parsing succeeds."""
        branch = BitbucketBranch.from_api_response(
            {**_BRANCH_API, "metadata": {"x": 1}, "futureField": True}
        )
        assert branch.display_id == "main"
        assert "metadata" not in branch.to_simplified_dict()

        tag = BitbucketTag.from_api_response(
            {**_TAG_API, "metadata": {"x": 1}, "futureField": True}
        )
        assert tag.display_id == "v1.0.0"
        assert "metadata" not in tag.to_simplified_dict()

    def test_directory_entry_parses_wire_shape(self):
        """Browse children carry the {components, name, parent} path object and
        the FILE/DIRECTORY/SUBMODULE type; extra wire fields (e.g. a richer
        `path` object) are ignored."""
        entry = BitbucketDirectoryEntry.from_api_response(
            {
                "path": {
                    "components": ["src", "app.py"],
                    "parent": "src",
                    "name": "app.py",
                    "extension": "py",
                },
                "type": "FILE",
                "size": 64,
                "contentId": "c9",
                "futureField": {"nested": 1},
            }
        )
        assert entry.path == "src/app.py"
        assert entry.type == "FILE"
        assert entry.size == 64
        assert entry.content_id == "c9"
        assert "futureField" not in entry.to_simplified_dict()


class TestSummaryProjections:
    """to_summary_dict, the minimal identity projection for list triage."""

    def test_project_summary_is_identity_only(self):
        result = BitbucketProject.from_api_response(_PROJECT_API).to_summary_dict()
        assert result == {"key": "PROJ", "name": "My Project"}

    def test_repository_summary_is_identity_only(self):
        result = BitbucketRepository.from_api_response(_REPO_API).to_summary_dict()
        assert result == {"slug": "my-repo", "name": "My Repo"}

    def test_pull_request_summary_carries_triage_fields(self):
        result = BitbucketPullRequest.from_api_response(_PR_API).to_summary_dict()
        assert result["id"] == 42
        assert result["title"] == "Add feature X"
        assert result["state"] == "OPEN"
        assert result["author"]["user"]["name"] == "auth1"
        # The bulk fields of the full projection are absent.
        for absent in ("reviewers", "description", "from_ref", "to_ref", "version"):
            assert absent not in result

    def test_pull_request_summary_omits_absent_author(self):
        data = {k: v for k, v in _PR_API.items() if k != "author"}
        data["participants"] = []
        result = BitbucketPullRequest.from_api_response(data).to_summary_dict()
        assert result["id"] == 42
        assert "author" not in result


class TestBitbucketChange:
    """The pull-request changed-file model (a RestChange entry)."""

    def test_from_api_response_projects_discovery_fields(self):
        change = BitbucketChange.from_api_response(
            {
                "path": {"components": ["src", "new.py"], "name": "new.py"},
                "srcPath": {"components": ["src", "old.py"], "name": "old.py"},
                "type": "MOVE",
                "nodeType": "FILE",
                "executable": True,
                "percentUnchanged": 98,
                "contentId": "f1ce" + "0" * 36,
                "fromContentId": "f1ce" + "1" * 36,
            }
        )
        assert change.path == "src/new.py"
        assert change.src_path == "src/old.py"
        assert change.type == "MOVE"
        assert change.node_type == "FILE"
        assert change.executable is True
        assert change.percent_unchanged == 98
        assert change.to_simplified_dict() == {
            "path": "src/new.py",
            "src_path": "src/old.py",
            "type": "MOVE",
            "node_type": "FILE",
            "executable": True,
            "percent_unchanged": 98,
        }

    def test_simplified_dict_omits_absent_fields(self):
        change = BitbucketChange.from_api_response(
            {
                "path": {"components": ["a.py"], "name": "a.py"},
                "type": "ADD",
                "nodeType": "FILE",
            }
        )
        assert change.to_simplified_dict() == {
            "path": "a.py",
            "type": "ADD",
            "node_type": "FILE",
        }

    def test_conflict_object_becomes_flag(self):
        change = BitbucketChange.from_api_response(
            {
                "path": {"components": ["a.py"], "name": "a.py"},
                "type": "MODIFY",
                "conflict": {"ourChange": {}, "theirChange": {}},
            }
        )
        assert change.conflict is True
        assert change.to_simplified_dict()["conflict"] is True

    def test_empty_entry_defaults_and_wrong_typed_similarity_raises(self):
        assert BitbucketChange.from_api_response({}).to_simplified_dict() == {}
        for bad in ("98", True):
            with pytest.raises(ValueError, match="'percentUnchanged' in"):
                BitbucketChange.from_api_response({"percentUnchanged": bad})


class TestScalarFieldTypes:
    """A wrong-typed scalar raises one message form; null takes the default."""

    @pytest.mark.parametrize(
        ("model", "body", "key", "expected"),
        [
            (BitbucketProject, {"key": "P", "name": 5}, "name", "a string"),
            (BitbucketProject, {"key": "P", "public": "no"}, "public", "a boolean"),
            (
                BitbucketRepository,
                {"slug": "r", "archived": "false"},
                "archived",
                "a boolean",
            ),
            (
                BitbucketBranch,
                {"id": "refs/heads/x", "isDefault": 1},
                "isDefault",
                "a boolean",
            ),
            (BitbucketTag, {"id": "refs/tags/x", "hash": 1}, "hash", "a string"),
            (
                BitbucketCommit,
                {"id": "sha", "authorTimestamp": "yesterday"},
                "authorTimestamp",
                "an integer",
            ),
            (BitbucketPullRequest, {"id": 7, "state": 5}, "state", "a string"),
            (BitbucketPullRequest, {"id": "7"}, "id", "an integer"),
            (BitbucketPullRequest, {"id": 7, "version": True}, "version", "an integer"),
            (
                BitbucketActivity,
                {"id": 1, "createdDate": "yesterday"},
                "createdDate",
                "an integer",
            ),
            (BitbucketComment, {"id": 1, "severity": 9}, "severity", "a string"),
            (BitbucketChange, {"type": 1}, "type", "a string"),
            (
                BitbucketDirectoryEntry,
                {"type": "FILE", "size": "12"},
                "size",
                "an integer",
            ),
            (BitbucketUser, {"name": ["x"]}, "name", "a string"),
            (
                BitbucketFileDiff,
                {"hunks": [{"sourceLine": "x"}]},
                "sourceLine",
                "an integer",
            ),
            (BitbucketFileDiff, {"binary": "yes"}, "binary", "a boolean"),
        ],
    )
    def test_wrong_typed_scalar_raises(self, model, body, key, expected):
        with pytest.raises(ValueError) as excinfo:
            model.from_api_response(body)
        message = str(excinfo.value)
        assert message.startswith("Bitbucket returned an unexpected response shape:")
        assert f"'{key}' in " in message
        assert message.endswith(f"not {expected}.")

    def test_null_scalars_take_defaults(self):
        pr = BitbucketPullRequest.from_api_response(
            {"id": None, "title": None, "state": None, "draft": None, "version": None}
        )
        assert pr.id == 0
        assert pr.title == "Unknown"
        assert pr.state is None
        assert pr.draft is None
        assert pr.version is None
        branch = BitbucketBranch.from_api_response(
            {"id": "refs/heads/main", "isDefault": None, "default": True}
        )
        assert branch.is_default is True
