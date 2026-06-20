"""Bitbucket Data Center pull-request models."""

import logging
from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN
from .user import BitbucketUser

logger = logging.getLogger(__name__)


class BitbucketRef(ApiModel):
    """A Bitbucket pull-request ref (the ``fromRef``/``toRef`` endpoints).

    Models the read-relevant fields of the ref object in ``RestPullRequest``:
    the fully-qualified ref id, its short display id, and the latest commit. The
    nested ``repository`` object is not modelled here (the PR already carries the
    project/repo context through the tool's path parameters).
    """

    id: str = EMPTY_STRING
    display_id: str = EMPTY_STRING
    latest_commit: str | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any], **kwargs: Any) -> "BitbucketRef":
        """Create a BitbucketRef from a Bitbucket API ref object.

        Args:
            data: The ``fromRef``/``toRef`` object from the Bitbucket API.

        Returns:
            A BitbucketRef instance (a default instance for empty/non-dict
            input).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        return cls(
            id=str(data.get("id") or EMPTY_STRING),
            display_id=str(data.get("displayId") or EMPTY_STRING),
            latest_commit=data.get("latestCommit"),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.display_id:
            result["display_id"] = self.display_id
        if self.id:
            result["id"] = self.id
        if self.latest_commit:
            result["latest_commit"] = self.latest_commit
        return result


class BitbucketParticipant(ApiModel):
    """A Bitbucket pull-request participant (``RestPullRequestParticipant``).

    Carries the participant's user, their role (AUTHOR/REVIEWER/PARTICIPANT) and
    review status (UNAPPROVED/NEEDS_WORK/APPROVED) — the approval state a
    reviewer needs.
    """

    user: BitbucketUser | None = None
    role: str | None = None
    status: str | None = None
    approved: bool | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketParticipant":
        """Create a BitbucketParticipant from a Bitbucket API participant object.

        Args:
            data: The participant object from the Bitbucket API.

        Returns:
            A BitbucketParticipant instance (a default instance for empty/non-dict
            input). A malformed nested user degrades to ``None`` rather than
            raising.
        """
        if not isinstance(data, dict) or not data:
            return cls()
        user_data = data.get("user")
        user = (
            BitbucketUser.from_api_response(user_data)
            if isinstance(user_data, dict)
            else None
        )
        return cls(
            user=user,
            role=data.get("role"),
            status=data.get("status"),
            approved=data.get("approved"),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.user is not None:
            result["user"] = self.user.to_simplified_dict()
        if self.role:
            result["role"] = self.role
        if self.status:
            result["status"] = self.status
        if self.approved is not None:
            result["approved"] = self.approved
        return result


class BitbucketPullRequest(ApiModel):
    """A Bitbucket Data Center pull request (``RestPullRequest``).

    Models the read-relevant metadata of a pull request: identity, state, the
    source/target refs, and the reviewer/approval timeline. The runtime carries a
    top-level ``author`` (a ``RestPullRequestParticipant``); when it is absent the
    author falls back to the ``participants`` entry whose role is ``AUTHOR``. The
    write-only ``links`` object is not modelled. Epoch-millisecond timestamps are
    surfaced unchanged.
    """

    id: int = 0
    title: str = UNKNOWN
    state: str | None = None
    description: str | None = None
    draft: bool | None = None
    open: bool | None = None
    closed: bool | None = None
    locked: bool | None = None
    version: int | None = None
    created_date: int | None = None
    updated_date: int | None = None
    closed_date: int | None = None
    from_ref: BitbucketRef | None = None
    to_ref: BitbucketRef | None = None
    author: BitbucketParticipant | None = None
    reviewers: list[BitbucketParticipant] = []
    participants: list[BitbucketParticipant] = []

    @staticmethod
    def _participants(data: dict[str, Any], field: str) -> list[BitbucketParticipant]:
        """Build the participant list for ``field`` (reviewers/participants)."""
        raw = data.get(field)
        if not isinstance(raw, list):
            return []
        return [
            BitbucketParticipant.from_api_response(item)
            for item in raw
            if isinstance(item, dict)
        ]

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketPullRequest":
        """Create a BitbucketPullRequest from a Bitbucket API response.

        Args:
            data: The pull-request object from the Bitbucket API.

        Returns:
            A BitbucketPullRequest instance (a default instance for empty/non-dict
            input).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        pr_id = data.get("id")
        from_ref_data = data.get("fromRef")
        to_ref_data = data.get("toRef")
        reviewers = cls._participants(data, "reviewers")
        participants = cls._participants(data, "participants")
        # Prefer the top-level author; fall back to the AUTHOR-role participant.
        # An empty object counts as absent, so the fallback still fires.
        top_author = data.get("author")
        author = (
            BitbucketParticipant.from_api_response(top_author)
            if isinstance(top_author, dict) and top_author
            else next((p for p in participants if p.role == "AUTHOR"), None)
        )
        return cls(
            id=int(pr_id) if isinstance(pr_id, int) else 0,
            title=str(data.get("title") or UNKNOWN),
            state=data.get("state"),
            description=data.get("description"),
            draft=data.get("draft"),
            open=data.get("open"),
            closed=data.get("closed"),
            locked=data.get("locked"),
            version=data.get("version"),
            created_date=data.get("createdDate"),
            updated_date=data.get("updatedDate"),
            closed_date=data.get("closedDate"),
            from_ref=(
                BitbucketRef.from_api_response(from_ref_data)
                if isinstance(from_ref_data, dict)
                else None
            ),
            to_ref=(
                BitbucketRef.from_api_response(to_ref_data)
                if isinstance(to_ref_data, dict)
                else None
            ),
            author=author,
            reviewers=reviewers,
            participants=participants,
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"id": self.id, "title": self.title}
        if self.state:
            result["state"] = self.state
        if self.draft is not None:
            result["draft"] = self.draft
        if self.description:
            result["description"] = self.description
        if self.from_ref is not None:
            from_ref = self.from_ref.to_simplified_dict()
            if from_ref:
                result["from_ref"] = from_ref
        if self.to_ref is not None:
            to_ref = self.to_ref.to_simplified_dict()
            if to_ref:
                result["to_ref"] = to_ref
        if self.author is not None:
            author = self.author.to_simplified_dict()
            if author:
                result["author"] = author
        if self.reviewers:
            result["reviewers"] = [r.to_simplified_dict() for r in self.reviewers]
        if self.created_date is not None:
            result["created_date"] = self.created_date
        if self.updated_date is not None:
            result["updated_date"] = self.updated_date
        if self.version is not None:
            result["version"] = self.version
        return result

    def to_summary_dict(self) -> dict[str, Any]:
        """Minimal projection for list triage.

        Carries the fields needed to choose a pull request (``id``, ``title``,
        ``state``, ``author``) before fetching its full detail with
        ``get_pull_request``. Used by ``list_pull_requests(summary=True)``.
        """
        result: dict[str, Any] = {"id": self.id, "title": self.title}
        if self.state:
            result["state"] = self.state
        if self.author is not None:
            author = self.author.to_simplified_dict()
            if author:
                result["author"] = author
        return result
