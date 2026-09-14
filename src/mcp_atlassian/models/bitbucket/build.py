"""Bitbucket Data Center build-status models."""

from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING
from ._fields import _opt_int, _opt_str


class BitbucketTestResults(ApiModel):
    """The test counts attached to a build status (``testResults``).

    Each count is optional on the wire. A CI system that reports no test
    breakdown leaves the whole object absent.
    """

    successful: int | None = None
    failed: int | None = None
    skipped: int | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketTestResults":
        """Create a BitbucketTestResults from a ``testResults`` object.

        Args:
            data: The ``testResults`` object from the Bitbucket API.

        Returns:
            A BitbucketTestResults instance (a default instance for
            empty/non-dict input).

        Raises:
            ValueError: If a count is present with a value of the wrong type
                (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        return cls(
            successful=_opt_int(data, "successful", model=model),
            failed=_opt_int(data, "failed", model=model),
            skipped=_opt_int(data, "skipped", model=model),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.successful is not None:
            result["successful"] = self.successful
        if self.failed is not None:
            result["failed"] = self.failed
        if self.skipped is not None:
            result["skipped"] = self.skipped
        return result


class BitbucketBuildStatus(ApiModel):
    """A Bitbucket Data Center build status (``RestBuildStatus``).

    Models a CI status posted against a commit, as returned by
    ``GET /rest/build-status/1.0/commits/{commitId}``: the CI system's plan
    ``key`` and human ``name``, the ``state``, the link to the build, the
    optional ``description``, ``build_number``, ``duration`` (milliseconds),
    the ``ref`` the build ran against, the ``parent`` plan key, the
    repository the status was posted through, the epoch-millisecond
    timestamps, and the optional test counts. Every field is optional on the
    wire. The 9.4 OpenAPI specification lists none as required. An absent,
    null, empty, or non-object ``testResults`` leaves ``test_results`` unset.
    """

    key: str = EMPTY_STRING
    state: str = EMPTY_STRING
    name: str | None = None
    url: str | None = None
    description: str | None = None
    build_number: str | None = None
    duration: int | None = None
    ref: str | None = None
    parent: str | None = None
    project_key: str | None = None
    repository_slug: str | None = None
    created_date: int | None = None
    updated_date: int | None = None
    test_results: BitbucketTestResults | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketBuildStatus":
        """Create a BitbucketBuildStatus from a Bitbucket API build-status object.

        Args:
            data: The build-status object (``RestBuildStatus``) from the
                Bitbucket API.

        Returns:
            A BitbucketBuildStatus instance (a default instance for
            empty/non-dict input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        test_results_data = data.get("testResults")
        return cls(
            key=_opt_str(data, "key", model=model) or EMPTY_STRING,
            state=_opt_str(data, "state", model=model) or EMPTY_STRING,
            name=_opt_str(data, "name", model=model),
            url=_opt_str(data, "url", model=model),
            description=_opt_str(data, "description", model=model),
            build_number=_opt_str(data, "buildNumber", model=model),
            duration=_opt_int(data, "duration", model=model),
            ref=_opt_str(data, "ref", model=model),
            parent=_opt_str(data, "parent", model=model),
            project_key=_opt_str(data, "projectKey", model=model),
            repository_slug=_opt_str(data, "repositorySlug", model=model),
            created_date=_opt_int(data, "createdDate", model=model),
            updated_date=_opt_int(data, "updatedDate", model=model),
            test_results=(
                BitbucketTestResults.from_api_response(test_results_data)
                if isinstance(test_results_data, dict) and test_results_data
                else None
            ),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.key:
            result["key"] = self.key
        if self.state:
            result["state"] = self.state
        if self.name:
            result["name"] = self.name
        if self.url:
            result["url"] = self.url
        if self.description:
            result["description"] = self.description
        if self.build_number:
            result["build_number"] = self.build_number
        if self.duration is not None:
            result["duration"] = self.duration
        if self.ref:
            result["ref"] = self.ref
        if self.parent:
            result["parent"] = self.parent
        if self.project_key:
            result["project_key"] = self.project_key
        if self.repository_slug:
            result["repository_slug"] = self.repository_slug
        if self.created_date is not None:
            result["created_date"] = self.created_date
        if self.updated_date is not None:
            result["updated_date"] = self.updated_date
        if self.test_results is not None:
            result["test_results"] = self.test_results.to_simplified_dict()
        return result
