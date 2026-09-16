"""Bitbucket Data Center data models for the MCP Atlassian integration.

Pydantic models for Bitbucket Data Center API data structures, organized by
entity type, mirroring the Jira and Confluence model packages.

Malformed-data contract shared by every ``from_api_response`` here. A
nested collection (``reviewers``, ``participants``, ``parents``, ``comments``,
``vetoes``, and the diff's ``hunks``, ``segments``, ``lines``) that is absent
or null is empty, since a binary or server-truncated file carries no ``hunks``
and the server omits a list with nothing to report. A collection that is present
with a value other than null or a list, or that holds a non-object entry,
raises ``ValueError``, because dropping entries would report a corrupt body
as a complete one. The top-level ``diffs`` key must be a list in a non-empty
body. A null or absent ``diffs`` raises there.
An optional scalar or nested object that is absent or null takes the field's
default; a scalar that is present with a value of another type raises
``ValueError`` through the readers in ``_fields``.
"""

from .activity import BitbucketActivity, BitbucketComment
from .build import BitbucketBuildStatus, BitbucketTestResults
from .change import BitbucketChange
from .commit import BitbucketCommit
from .diff import (
    BitbucketDiffHunk,
    BitbucketDiffLine,
    BitbucketDiffSegment,
    BitbucketFileDiff,
    BitbucketPullRequestDiff,
)
from .merge_status import BitbucketMergeStatus, BitbucketMergeVeto
from .project import BitbucketProject
from .pull_request import (
    BitbucketParticipant,
    BitbucketPullRequest,
    BitbucketRef,
)
from .ref import BitbucketBranch, BitbucketTag
from .repository import BitbucketRepository
from .source import BitbucketDirectoryEntry
from .user import BitbucketUser, BitbucketUserProfile

__all__ = [
    "BitbucketActivity",
    "BitbucketBranch",
    "BitbucketBuildStatus",
    "BitbucketChange",
    "BitbucketComment",
    "BitbucketCommit",
    "BitbucketDiffHunk",
    "BitbucketDiffLine",
    "BitbucketDiffSegment",
    "BitbucketDirectoryEntry",
    "BitbucketFileDiff",
    "BitbucketMergeStatus",
    "BitbucketMergeVeto",
    "BitbucketParticipant",
    "BitbucketProject",
    "BitbucketPullRequest",
    "BitbucketPullRequestDiff",
    "BitbucketRef",
    "BitbucketRepository",
    "BitbucketTag",
    "BitbucketTestResults",
    "BitbucketUser",
    "BitbucketUserProfile",
]
