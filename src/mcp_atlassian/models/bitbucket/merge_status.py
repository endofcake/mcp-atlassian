"""Bitbucket Data Center pull-request mergeability models."""

from typing import Any

from ..base import ApiModel
from ._fields import _opt_bool, _opt_list, _opt_str


class BitbucketMergeVeto(ApiModel):
    """One merge-check veto on a pull request (``RestRepositoryHookVeto``).

    A merge check (a repository hook such as a required-reviewers or
    required-builds check) that blocks the merge reports a short summary and
    a longer detail message.
    """

    summary: str | None = None
    detail: str | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketMergeVeto":
        """Create a BitbucketMergeVeto from a Bitbucket API veto object.

        Args:
            data: The veto object (``RestRepositoryHookVeto``) from the
                Bitbucket API.

        Returns:
            A BitbucketMergeVeto instance (a default instance for empty/non-dict
            input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        return cls(
            summary=_opt_str(data, "summaryMessage", model=model),
            detail=_opt_str(data, "detailedMessage", model=model),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.summary:
            result["summary"] = self.summary
        if self.detail:
            result["detail"] = self.detail
        return result


class BitbucketMergeStatus(ApiModel):
    """A pull request's mergeability (``RestPullRequestMergeability``).

    Models the body of ``GET .../pull-requests/{id}/merge``: whether the
    source and target conflict, the overall ``outcome`` (``CLEAN``,
    ``CONFLICTED``, or ``UNKNOWN`` in the Bitbucket Data Center 9.4 REST
    specification, passed through unvalidated so a new server value does not
    break the read), and the merge checks that veto the merge. ``canMerge``
    is emitted by the server but is not documented in the specification, so
    it is optional. The specification is published at
    https://developer.atlassian.com/server/bitbucket/rest/v904/.
    """

    can_merge: bool | None = None
    conflicted: bool | None = None
    outcome: str | None = None
    vetoes: list[BitbucketMergeVeto] = []

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketMergeStatus":
        """Create a BitbucketMergeStatus from a Bitbucket API mergeability object.

        Args:
            data: The mergeability object (``RestPullRequestMergeability``)
                from the Bitbucket API.

        Returns:
            A BitbucketMergeStatus instance (a default instance for
            empty/non-dict input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type, or if ``vetoes`` is present and is not a list of objects
                (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        return cls(
            can_merge=_opt_bool(data, "canMerge", model=model),
            conflicted=_opt_bool(data, "conflicted", model=model),
            outcome=_opt_str(data, "outcome", model=model),
            vetoes=[
                BitbucketMergeVeto.from_api_response(item)
                for item in _opt_list(data, "vetoes", model=model)
            ],
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses.

        ``vetoes`` is always present. It is empty when the server reports no
        vetoes or omits the field. The optional scalars are omitted when the
        server did not report them.
        """
        result: dict[str, Any] = {}
        if self.can_merge is not None:
            result["can_merge"] = self.can_merge
        if self.conflicted is not None:
            result["conflicted"] = self.conflicted
        if self.outcome:
            result["outcome"] = self.outcome
        result["vetoes"] = [veto.to_simplified_dict() for veto in self.vetoes]
        return result
