"""Bitbucket Data Center data models for the MCP Atlassian integration.

Pydantic models for Bitbucket Data Center API data structures, organized by
entity type, mirroring the Jira and Confluence model packages.
"""

from .activity import BitbucketActivity, BitbucketComment
from .diff import (
    BitbucketDiffHunk,
    BitbucketDiffLine,
    BitbucketDiffSegment,
    BitbucketFileDiff,
    BitbucketPullRequestDiff,
)
from .project import BitbucketProject
from .pull_request import (
    BitbucketParticipant,
    BitbucketPullRequest,
    BitbucketRef,
)
from .repository import BitbucketRepository
from .user import BitbucketUser

__all__ = [
    "BitbucketActivity",
    "BitbucketComment",
    "BitbucketDiffHunk",
    "BitbucketDiffLine",
    "BitbucketDiffSegment",
    "BitbucketFileDiff",
    "BitbucketParticipant",
    "BitbucketProject",
    "BitbucketPullRequest",
    "BitbucketPullRequestDiff",
    "BitbucketRef",
    "BitbucketRepository",
    "BitbucketUser",
]
