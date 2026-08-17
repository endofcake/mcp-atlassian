"""Bitbucket Data Center repository models."""

from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING, UNKNOWN
from ._fields import _opt_bool, _opt_str
from .project import BitbucketProject


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
            input). A missing nested project is None.

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
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
        model = cls.__name__
        return cls(
            slug=_opt_str(data, "slug", model=model) or EMPTY_STRING,
            id=str(repo_id) if repo_id is not None else EMPTY_STRING,
            name=_opt_str(data, "name", model=model) or UNKNOWN,
            description=_opt_str(data, "description", model=model),
            scm_id=_opt_str(data, "scmId", model=model),
            state=_opt_str(data, "state", model=model),
            forkable=_opt_bool(data, "forkable", model=model),
            archived=_opt_bool(data, "archived", model=model),
            public=_opt_bool(data, "public", model=model),
            default_branch=_opt_str(data, "defaultBranch", model=model),
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

    def to_summary_dict(self) -> dict[str, Any]:
        """Minimal projection for list triage: the identity fields only.

        Lets a caller scan many repositories to choose one (by ``slug``),
        then fetch full detail separately.
        """
        return {"slug": self.slug, "name": self.name}
