"""Bitbucket Data Center branch and tag models."""

from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING
from ._fields import _opt_bool, _opt_str


class BitbucketBranch(ApiModel):
    """A Bitbucket Data Center branch (``RestBranch``).

    Models the read-relevant fields of a branch returned by
    ``GET .../branches``: the fully-qualified ref id, its short display id, the
    latest commit, and whether it is the repository's default branch. The
    legacy ``latestChangeset`` alias (a duplicate of ``latestCommit``) is not
    modelled, so each value has one field. Also reused for the ``RestMinimalRef``
    returned by ``GET .../default-branch``, which populates only ``id`` /
    ``display_id`` / ``type`` (no commit).
    """

    id: str = EMPTY_STRING
    display_id: str = EMPTY_STRING
    latest_commit: str | None = None
    type: str | None = None
    is_default: bool | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketBranch":
        """Create a BitbucketBranch from a Bitbucket API branch object.

        Args:
            data: The branch object (``RestBranch`` or ``RestMinimalRef``) from
                the Bitbucket API.

        Returns:
            A BitbucketBranch instance (a default instance for empty/non-dict
            input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        # The live wire emits ``isDefault``; the OpenAPI specification
        # documents ``default``. Read the runtime key first and fall back to
        # the specification key, including when ``isDefault`` is null.
        is_default = _opt_bool(data, "isDefault", model=model)
        if is_default is None:
            is_default = _opt_bool(data, "default", model=model)
        return cls(
            id=_opt_str(data, "id", model=model) or EMPTY_STRING,
            display_id=_opt_str(data, "displayId", model=model) or EMPTY_STRING,
            latest_commit=_opt_str(data, "latestCommit", model=model),
            type=_opt_str(data, "type", model=model),
            is_default=is_default,
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
        if self.type:
            result["type"] = self.type
        if self.is_default is not None:
            result["is_default"] = self.is_default
        return result

    def to_summary_dict(self) -> dict[str, Any]:
        """Minimal projection for list-row triage: display id and latest commit."""
        result: dict[str, Any] = {}
        if self.display_id:
            result["display_id"] = self.display_id
        if self.latest_commit:
            result["latest_commit"] = self.latest_commit
        return result


class BitbucketTag(ApiModel):
    """A Bitbucket Data Center tag (``RestTag``).

    Models the read-relevant fields of a tag returned by ``GET .../tags`` and
    ``GET .../tags/{name}``: the fully-qualified ref id, its short display id,
    the commit the tag points at, and ``hash`` (the annotated-tag object SHA,
    null for a lightweight tag). The legacy ``latestChangeset`` alias is not
    modelled. A tag has no default-branch flag.
    """

    id: str = EMPTY_STRING
    display_id: str = EMPTY_STRING
    latest_commit: str | None = None
    type: str | None = None
    hash: str | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any], **kwargs: Any) -> "BitbucketTag":
        """Create a BitbucketTag from a Bitbucket API tag object.

        Args:
            data: The tag object (``RestTag``) from the Bitbucket API.

        Returns:
            A BitbucketTag instance (a default instance for empty/non-dict
            input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        return cls(
            id=_opt_str(data, "id", model=model) or EMPTY_STRING,
            display_id=_opt_str(data, "displayId", model=model) or EMPTY_STRING,
            latest_commit=_opt_str(data, "latestCommit", model=model),
            type=_opt_str(data, "type", model=model),
            hash=_opt_str(data, "hash", model=model),
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
        if self.type:
            result["type"] = self.type
        if self.hash:
            result["hash"] = self.hash
        return result

    def to_summary_dict(self) -> dict[str, Any]:
        """Minimal projection for list-row triage: display id and latest commit."""
        result: dict[str, Any] = {}
        if self.display_id:
            result["display_id"] = self.display_id
        if self.latest_commit:
            result["latest_commit"] = self.latest_commit
        return result
