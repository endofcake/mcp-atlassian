"""Bitbucket Data Center user models."""

import logging
from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN

logger = logging.getLogger(__name__)


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
            input, so a malformed nested user never raises).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        # ``or`` (not a get-default) so an explicit JSON null falls back to the
        # sentinel instead of stringifying to the literal "None". The DC user
        # object carries both ``name`` (username) and ``slug`` (URL form); prefer
        # ``name`` and fall back to ``slug``.
        return cls(
            name=str(data.get("name") or data.get("slug") or EMPTY_STRING),
            display_name=str(data.get("displayName") or UNKNOWN),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"name": self.name}
        if self.display_name and self.display_name != UNKNOWN:
            result["display_name"] = self.display_name
        return result
