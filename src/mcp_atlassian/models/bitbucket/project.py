"""Bitbucket Data Center project models."""

import logging
from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN

logger = logging.getLogger(__name__)


class BitbucketProject(ApiModel):
    """A Bitbucket Data Center project.

    Models the read-relevant fields of the REST API ``RestProject`` object
    returned by the projects endpoint and nested in repository responses.
    """

    key: str = EMPTY_STRING
    id: str = EMPTY_STRING
    name: str = UNKNOWN
    description: str | None = None
    type: str | None = None
    public: bool | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketProject":
        """Create a BitbucketProject from a Bitbucket API response.

        Args:
            data: The project object from the Bitbucket API.

        Returns:
            A BitbucketProject instance (a default instance for empty/non-dict
            input, so a malformed nested project never raises).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        project_id = data.get("id")
        return cls(
            # ``or`` (not a get-default) so an explicit JSON null falls back to
            # the sentinel instead of stringifying to the literal "None".
            key=str(data.get("key") or EMPTY_STRING),
            id=str(project_id) if project_id is not None else EMPTY_STRING,
            name=str(data.get("name") or UNKNOWN),
            description=data.get("description"),
            type=data.get("type"),
            public=data.get("public"),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"key": self.key, "name": self.name}
        if self.description:
            result["description"] = self.description
        if self.type:
            result["type"] = self.type
        if self.public is not None:
            result["public"] = self.public
        return result

    def to_summary_dict(self) -> dict[str, Any]:
        """Minimal projection for list triage: the identity fields only.

        Used by ``list_projects(summary=True)`` so a caller can scan many
        projects to choose one (by ``key``), then fetch full detail separately.
        """
        return {"key": self.key, "name": self.name}
