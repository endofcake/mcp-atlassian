"""Bitbucket Data Center pull-request activity and comment models."""

from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING
from ._fields import _opt_bool, _opt_int, _opt_str
from .user import BitbucketUser


class BitbucketComment(ApiModel):
    """A Bitbucket pull-request comment (``RestComment``).

    Models the read-relevant fields of a comment: its id, version, text, author,
    creation timestamp, and thread state. Nested replies (the ``comments``
    array) are not recursed here; ``reply_count`` signals their presence so a
    reviewer knows a thread exists without inflating the payload. ``version`` is
    the optimistic-lock token a subsequent edit/delete must echo, so it is
    surfaced on the comment returned by a write.
    """

    id: int = 0
    version: int | None = None
    text: str = EMPTY_STRING
    author: BitbucketUser | None = None
    created_date: int | None = None
    state: str | None = None
    severity: str | None = None
    thread_resolved: bool | None = None
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

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        model = cls.__name__
        author_data = data.get("author")
        author = (
            BitbucketUser.from_api_response(author_data)
            if isinstance(author_data, dict)
            else None
        )
        replies = data.get("comments")
        reply_count = len(replies) if isinstance(replies, list) else 0
        return cls(
            id=_opt_int(data, "id", model=model) or 0,
            version=_opt_int(data, "version", model=model),
            text=_opt_str(data, "text", model=model) or EMPTY_STRING,
            author=author,
            created_date=_opt_int(data, "createdDate", model=model),
            state=_opt_str(data, "state", model=model),
            severity=_opt_str(data, "severity", model=model),
            thread_resolved=_opt_bool(data, "threadResolved", model=model),
            reply_count=reply_count,
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"id": self.id, "text": self.text}
        # version can legitimately be 0 (a freshly created comment), so it is
        # tested for presence rather than truthiness.
        if self.version is not None:
            result["version"] = self.version
        if self.author is not None:
            result["author"] = self.author.to_simplified_dict()
        if self.created_date is not None:
            result["created_date"] = self.created_date
        if self.state:
            result["state"] = self.state
        if self.severity:
            result["severity"] = self.severity
        # thread_resolved can legitimately be False (an open thread), so it is
        # tested for presence rather than truthiness, which confirms a
        # resolve/unresolve result.
        if self.thread_resolved is not None:
            result["thread_resolved"] = self.thread_resolved
        if self.reply_count:
            result["reply_count"] = self.reply_count
        return result


class BitbucketActivity(ApiModel):
    """A Bitbucket pull-request activity entry (``RestPullRequestActivity``).

    Models one item in the PR activity timeline: its id, the action
    (OPENED/APPROVED/COMMENTED/MERGED/…), the actor, and the timestamp. For
    ``COMMENTED`` actions the activity carries a ``comment`` payload
    (``RestComment``); the base ``RestPullRequestActivity`` schema does not
    enumerate that field, so it is read defensively and surfaced when present.
    This is the documented behaviour of the activities timeline, pinned by the
    golden capture fixtures.
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

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        model = cls.__name__
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
            id=_opt_int(data, "id", model=model) or 0,
            action=_opt_str(data, "action", model=model),
            created_date=_opt_int(data, "createdDate", model=model),
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
