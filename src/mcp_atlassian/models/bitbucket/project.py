"""Bitbucket Data Center project models."""

from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN
from ._fields import _opt_bool, _opt_str


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
            input, so a missing nested project does not raise).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        model = cls.__name__
        project_id = data.get("id")
        return cls(
            key=_opt_str(data, "key", model=model) or EMPTY_STRING,
            id=str(project_id) if project_id is not None else EMPTY_STRING,
            name=_opt_str(data, "name", model=model) or UNKNOWN,
            description=_opt_str(data, "description", model=model),
            type=_opt_str(data, "type", model=model),
            public=_opt_bool(data, "public", model=model),
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

        Lets a caller scan many projects to choose one (by ``key``), then
        fetch full detail separately.
        """
        return {"key": self.key, "name": self.name}
