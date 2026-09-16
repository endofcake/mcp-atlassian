"""Bitbucket Data Center pull-request models."""

from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN
from ._fields import _opt_bool, _opt_int, _opt_list, _opt_str
from .user import BitbucketUser


class BitbucketRef(ApiModel):
    """A Bitbucket pull-request ref (the ``fromRef``/``toRef`` endpoints).

    Models the read-relevant fields of the ref object in ``RestPullRequest``:
    the fully-qualified ref id, its short display id, the latest commit, and
    the identity of the repository the ref lives in (``repository.slug`` and
    ``repository.project.key``). The repository identity is what lets a
    cross-repository listing be followed up with the repository-scoped tools,
    which take a project key and repository slug. The rest of the nested
    ``repository`` object is not modelled.
    """

    id: str = EMPTY_STRING
    display_id: str = EMPTY_STRING
    latest_commit: str | None = None
    project_key: str | None = None
    repository_slug: str | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any], **kwargs: Any) -> "BitbucketRef":
        """Create a BitbucketRef from a Bitbucket API ref object.

        Args:
            data: The ``fromRef``/``toRef`` object from the Bitbucket API.

        Returns:
            A BitbucketRef instance (a default instance for empty/non-dict
            input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        repository = data.get("repository")
        if not isinstance(repository, dict):
            repository = {}
        project = repository.get("project")
        if not isinstance(project, dict):
            project = {}
        return cls(
            id=_opt_str(data, "id", model=model) or EMPTY_STRING,
            display_id=_opt_str(data, "displayId", model=model) or EMPTY_STRING,
            latest_commit=_opt_str(data, "latestCommit", model=model),
            project_key=_opt_str(project, "key", model=model),
            repository_slug=_opt_str(repository, "slug", model=model),
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
        if self.project_key:
            result["project_key"] = self.project_key
        if self.repository_slug:
            result["repository_slug"] = self.repository_slug
        return result


class BitbucketParticipant(ApiModel):
    """A Bitbucket pull-request participant (``RestPullRequestParticipant``).

    Carries the participant's user, their role (AUTHOR/REVIEWER/PARTICIPANT) and
    review status (UNAPPROVED/NEEDS_WORK/APPROVED), the approval state a
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
            input). A missing nested user is ``None``.

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
        return cls(
            user=user,
            role=_opt_str(data, "role", model=model),
            status=_opt_str(data, "status", model=model),
            approved=_opt_bool(data, "approved", model=model),
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
        return [
            BitbucketParticipant.from_api_response(item)
            for item in _opt_list(data, field, model=BitbucketPullRequest.__name__)
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

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        model = cls.__name__
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
            id=_opt_int(data, "id", model=model) or 0,
            title=_opt_str(data, "title", model=model) or UNKNOWN,
            state=_opt_str(data, "state", model=model),
            description=_opt_str(data, "description", model=model),
            draft=_opt_bool(data, "draft", model=model),
            open=_opt_bool(data, "open", model=model),
            closed=_opt_bool(data, "closed", model=model),
            locked=_opt_bool(data, "locked", model=model),
            version=_opt_int(data, "version", model=model),
            created_date=_opt_int(data, "createdDate", model=model),
            updated_date=_opt_int(data, "updatedDate", model=model),
            closed_date=_opt_int(data, "closedDate", model=model),
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
        ``state``, ``author``) before fetching its full detail, plus the target
        repository's ``project_key`` and ``repository_slug`` when the response
        carries them, since a pull-request id is only unique within its
        repository and the detail tools take the repository as path parameters.
        """
        result: dict[str, Any] = {"id": self.id, "title": self.title}
        if self.state:
            result["state"] = self.state
        if self.to_ref is not None:
            if self.to_ref.project_key:
                result["project_key"] = self.to_ref.project_key
            if self.to_ref.repository_slug:
                result["repository_slug"] = self.to_ref.repository_slug
        if self.author is not None:
            author = self.author.to_simplified_dict()
            if author:
                result["author"] = author
        return result
