"""Bitbucket Data Center commit models."""

import logging
from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING
from .user import BitbucketUser

logger = logging.getLogger(__name__)


class BitbucketCommit(ApiModel):
    """A Bitbucket Data Center commit (``RestCommit``).

    Models the read-relevant fields of a commit returned by ``GET .../commits``,
    ``GET .../commits/{commitId}``, and ``GET .../pull-requests/{id}/commits``:
    the full and abbreviated ids, the message, the author/committer persons, the
    epoch-millisecond timestamps, and the parent ids.

    The ``author`` and ``committer`` are inline ``{name, emailAddress}`` objects
    (NOT user refs — they carry no ``slug``/account). They are parsed with
    :class:`~mcp_atlassian.models.bitbucket.BitbucketUser`, whose
    ``from_api_response`` reads ``name`` and deliberately drops ``emailAddress``,
    satisfying the PII-minimisation convention; a commit person carries no
    ``displayName``, so the display name is typically omitted from the output.
    Timestamps are passed through unformatted (epoch-ms ``int64``), consistent
    with the other Bitbucket models.
    """

    id: str = EMPTY_STRING
    display_id: str = EMPTY_STRING
    message: str = EMPTY_STRING
    parents: list[str] = []
    author_timestamp: int | None = None
    committer_timestamp: int | None = None
    author: BitbucketUser | None = None
    committer: BitbucketUser | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketCommit":
        """Create a BitbucketCommit from a Bitbucket API commit object.

        Args:
            data: The commit object (``RestCommit``) from the Bitbucket API.

        Returns:
            A BitbucketCommit instance (a default instance for empty/non-dict
            input, so a malformed commit never raises).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        author_data = data.get("author")
        committer_data = data.get("committer")
        parents_data = data.get("parents")
        parents = (
            [p["id"] for p in parents_data if isinstance(p, dict) and p.get("id")]
            if isinstance(parents_data, list)
            else []
        )
        return cls(
            id=str(data.get("id") or EMPTY_STRING),
            display_id=str(data.get("displayId") or EMPTY_STRING),
            message=str(data.get("message") or EMPTY_STRING),
            parents=parents,
            author_timestamp=data.get("authorTimestamp"),
            committer_timestamp=data.get("committerTimestamp"),
            author=(
                BitbucketUser.from_api_response(author_data)
                if isinstance(author_data, dict)
                else None
            ),
            committer=(
                BitbucketUser.from_api_response(committer_data)
                if isinstance(committer_data, dict)
                else None
            ),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.id:
            result["id"] = self.id
        if self.display_id:
            result["display_id"] = self.display_id
        if self.message:
            result["message"] = self.message
        if self.author is not None:
            result["author"] = self.author.to_simplified_dict()
        if self.author_timestamp is not None:
            result["author_timestamp"] = self.author_timestamp
        if self.committer is not None:
            result["committer"] = self.committer.to_simplified_dict()
        if self.committer_timestamp is not None:
            result["committer_timestamp"] = self.committer_timestamp
        if self.parents:
            result["parents"] = self.parents
        return result

    def to_summary_dict(self) -> dict[str, Any]:
        """Minimal projection for list-row triage.

        Carries the abbreviated id, author timestamp, author name, and the first
        line of the commit message — enough to scan a history and pick one.
        """
        result: dict[str, Any] = {}
        if self.display_id:
            result["display_id"] = self.display_id
        if self.author is not None and self.author.name:
            result["author"] = self.author.name
        if self.author_timestamp is not None:
            result["author_timestamp"] = self.author_timestamp
        if self.message:
            result["message"] = self.message.splitlines()[0]
        return result
