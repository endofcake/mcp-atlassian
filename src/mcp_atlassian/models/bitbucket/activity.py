"""Bitbucket Data Center pull-request activity and comment models."""

import logging
from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING
from .user import BitbucketUser

logger = logging.getLogger(__name__)


class BitbucketComment(ApiModel):
    """A Bitbucket pull-request comment (``RestComment``).

    Models the read-relevant fields of a comment: its id, text, author, creation
    timestamp, and thread state. Nested replies (the ``comments`` array) are not
    recursed here; ``reply_count`` signals their presence so a reviewer knows a
    thread exists without inflating the payload.
    """

    id: int = 0
    text: str = EMPTY_STRING
    author: BitbucketUser | None = None
    created_date: int | None = None
    state: str | None = None
    severity: str | None = None
    reply_count: int = 0

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketComment":
        """Create a BitbucketComment from a Bitbucket API comment object.

        Args:
            data: The ``RestComment`` object from the Bitbucket API.

        Returns:
            A BitbucketComment instance (a default instance for empty/non-dict
            input).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        comment_id = data.get("id")
        author_data = data.get("author")
        author = (
            BitbucketUser.from_api_response(author_data)
            if isinstance(author_data, dict)
            else None
        )
        replies = data.get("comments")
        reply_count = len(replies) if isinstance(replies, list) else 0
        return cls(
            id=int(comment_id) if isinstance(comment_id, int) else 0,
            text=str(data.get("text") or EMPTY_STRING),
            author=author,
            created_date=data.get("createdDate"),
            state=data.get("state"),
            severity=data.get("severity"),
            reply_count=reply_count,
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"id": self.id, "text": self.text}
        if self.author is not None:
            result["author"] = self.author.to_simplified_dict()
        if self.created_date is not None:
            result["created_date"] = self.created_date
        if self.state:
            result["state"] = self.state
        if self.severity:
            result["severity"] = self.severity
        if self.reply_count:
            result["reply_count"] = self.reply_count
        return result


class BitbucketActivity(ApiModel):
    """A Bitbucket pull-request activity entry (``RestPullRequestActivity``).

    Models one item in the PR activity timeline: its id, the action
    (OPENED/APPROVED/COMMENTED/MERGED/…), the actor, and the timestamp. For
    ``COMMENTED`` actions the activity carries a ``comment`` payload
    (``RestComment``); the base ``RestPullRequestActivity`` schema does not
    enumerate that field, so it is read defensively and surfaced when present —
    this is the documented behaviour of the activities timeline (the basis for
    the comments view) and is pinned by golden fixtures when live access lands.
    """

    id: int = 0
    action: str | None = None
    created_date: int | None = None
    user: BitbucketUser | None = None
    comment: BitbucketComment | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketActivity":
        """Create a BitbucketActivity from a Bitbucket API activity object.

        Args:
            data: The ``RestPullRequestActivity`` object from the Bitbucket API.

        Returns:
            A BitbucketActivity instance (a default instance for empty/non-dict
            input).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        activity_id = data.get("id")
        user_data = data.get("user")
        user = (
            BitbucketUser.from_api_response(user_data)
            if isinstance(user_data, dict)
            else None
        )
        comment_data = data.get("comment")
        comment = (
            BitbucketComment.from_api_response(comment_data)
            if isinstance(comment_data, dict)
            else None
        )
        return cls(
            id=int(activity_id) if isinstance(activity_id, int) else 0,
            action=data.get("action"),
            created_date=data.get("createdDate"),
            user=user,
            comment=comment,
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"id": self.id}
        if self.action:
            result["action"] = self.action
        if self.created_date is not None:
            result["created_date"] = self.created_date
        if self.user is not None:
            result["user"] = self.user.to_simplified_dict()
        if self.comment is not None:
            result["comment"] = self.comment.to_simplified_dict()
        return result
