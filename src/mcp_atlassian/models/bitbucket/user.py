"""Bitbucket Data Center user models."""

from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN
from ._fields import _opt_bool, _opt_int, _opt_str


class BitbucketUser(ApiModel):
    """A Bitbucket Data Center user reference.

    Models the read-relevant identity fields of the user object embedded in
    pull-request participants, activity actors, and comment authors (the REST
    ``RestApplicationUser`` shape). Only ``name`` (the username) and
    ``display_name`` are surfaced; ``emailAddress``, ``avatarUrl``, and ``links``
    are deliberately dropped to minimise PII and payload size in review output.
    """

    name: str = EMPTY_STRING
    display_name: str = UNKNOWN

    @classmethod
    def from_api_response(cls, data: dict[str, Any], **kwargs: Any) -> "BitbucketUser":
        """Create a BitbucketUser from a Bitbucket API user object.

        Args:
            data: The user object from the Bitbucket API.

        Returns:
            A BitbucketUser instance (a default instance for empty/non-dict
            input, so a missing nested user does not raise).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        # ``or`` rather than a ``get`` default, so an explicit JSON null falls
        # back to the sentinel instead of stringifying to "None". The DC user
        # object carries both ``name`` (username) and ``slug`` (URL form); prefer
        # ``name`` and fall back to ``slug``.
        model = cls.__name__
        name = _opt_str(data, "name", model=model)
        slug = _opt_str(data, "slug", model=model)
        return cls(
            name=name or slug or EMPTY_STRING,
            display_name=_opt_str(data, "displayName", model=model) or UNKNOWN,
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"name": self.name}
        if self.display_name and self.display_name != UNKNOWN:
            result["display_name"] = self.display_name
        return result


class BitbucketUserProfile(ApiModel):
    """A Bitbucket Data Center user profile (``RestApplicationUser``).

    Models the identity fields of the object returned by
    ``GET /users/{userSlug}``: the numeric ``id``, ``name`` (the username),
    ``slug`` (the URL form, which can differ from the username), the display
    name, whether the account is ``active``, and the account ``type``
    (``NORMAL`` or ``SERVICE``). ``emailAddress``, ``avatarUrl``, and ``links``
    are dropped, matching :class:`BitbucketUser`, because identity checks need
    only the username and slug, and keeping contact details out of tool output
    keeps them out of transcripts and logs.
    """

    id: int | None = None
    name: str = EMPTY_STRING
    slug: str = EMPTY_STRING
    display_name: str = UNKNOWN
    active: bool | None = None
    type: str | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketUserProfile":
        """Create a BitbucketUserProfile from a Bitbucket API user object.

        Args:
            data: The ``RestApplicationUser`` object from the Bitbucket API.

        Returns:
            A BitbucketUserProfile instance (a default instance for
            empty/non-dict input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        return cls(
            id=_opt_int(data, "id", model=model),
            name=_opt_str(data, "name", model=model) or EMPTY_STRING,
            slug=_opt_str(data, "slug", model=model) or EMPTY_STRING,
            display_name=_opt_str(data, "displayName", model=model) or UNKNOWN,
            active=_opt_bool(data, "active", model=model),
            type=_opt_str(data, "type", model=model),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"name": self.name, "slug": self.slug}
        if self.id is not None:
            result["id"] = self.id
        if self.display_name and self.display_name != UNKNOWN:
            result["display_name"] = self.display_name
        if self.active is not None:
            result["active"] = self.active
        if self.type:
            result["type"] = self.type
        return result
