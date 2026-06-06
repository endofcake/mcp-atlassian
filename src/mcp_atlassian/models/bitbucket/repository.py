"""Bitbucket Data Center repository models."""

import logging
from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN
from .project import BitbucketProject

logger = logging.getLogger(__name__)


class BitbucketRepository(ApiModel):
    """A Bitbucket Data Center repository.

    Models the read-relevant fields of the REST API ``RestRepository`` object
    returned by ``GET /rest/api/1.0/projects/{projectKey}/repos``. The ``links``
    object is write-only in the REST spec, so clone URLs are not modelled here.
    """

    slug: str = EMPTY_STRING
    id: str = EMPTY_STRING
    name: str = UNKNOWN
    description: str | None = None
    scm_id: str | None = None
    state: str | None = None
    forkable: bool | None = None
    archived: bool | None = None
    public: bool | None = None
    default_branch: str | None = None
    project: BitbucketProject | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketRepository":
        """Create a BitbucketRepository from a Bitbucket API response.

        Args:
            data: The repository object from the Bitbucket API.

        Returns:
            A BitbucketRepository instance (a default instance for empty/non-dict
            input). A malformed nested project degrades to a default project
            rather than raising.
        """
        if not isinstance(data, dict) or not data:
            return cls()

        repo_id = data.get("id")
        project_data = data.get("project")
        project = (
            BitbucketProject.from_api_response(project_data)
            if isinstance(project_data, dict)
            else None
        )
        return cls(
            # ``or`` (not a get-default) so an explicit JSON null falls back to
            # the sentinel instead of stringifying to the literal "None".
            slug=str(data.get("slug") or EMPTY_STRING),
            id=str(repo_id) if repo_id is not None else EMPTY_STRING,
            name=str(data.get("name") or UNKNOWN),
            description=data.get("description"),
            scm_id=data.get("scmId"),
            state=data.get("state"),
            forkable=data.get("forkable"),
            archived=data.get("archived"),
            public=data.get("public"),
            default_branch=data.get("defaultBranch"),
            project=project,
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"slug": self.slug, "name": self.name}
        if self.description:
            result["description"] = self.description
        if self.state:
            result["state"] = self.state
        if self.default_branch:
            result["default_branch"] = self.default_branch
        if self.scm_id:
            result["scm_id"] = self.scm_id
        if self.public is not None:
            result["public"] = self.public
        if self.archived is not None:
            result["archived"] = self.archived
        if self.forkable is not None:
            result["forkable"] = self.forkable
        if self.project is not None:
            result["project"] = self.project.to_simplified_dict()
        return result
