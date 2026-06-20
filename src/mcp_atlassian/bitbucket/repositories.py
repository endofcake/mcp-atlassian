"""Bitbucket Data Center repository operations."""

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..models.bitbucket import BitbucketRepository
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Default number of repositories a single list_repositories call returns.
DEFAULT_REPOS_LIMIT = 25
# Hard ceiling on repositories returned in one call. Bitbucket DC paginates the
# repos endpoint; this caps the per-call window (the tool's `limit` parameter),
# and the caller pages further with the returned cursor.
MAX_REPOS_LIMIT = 1000


@dataclass
class BitbucketRepositoriesPage:
    """A bounded, possibly-truncated view of a project's repositories.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage` for the full
    meaning of the ``is_last_page`` / ``truncated`` combinations).

    Attributes:
        repositories: The collected repository models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the project's list.
        truncated: Whether the returned ``repositories`` omits repos that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
    """

    repositories: list[BitbucketRepository]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


class ReposMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center repository operations."""

    def list_repositories(
        self,
        project_key: str,
        *,
        name: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_REPOS_LIMIT,
    ) -> BitbucketRepositoriesPage:
        """List repositories in a Bitbucket Data Center project.

        Without ``name`` this calls the project-scoped
        ``GET /rest/api/1.0/projects/{projectKey}/repos`` (paged with
        ``start``/``limit``), fetching a single window (``start`` → up to
        ``limit``) in one request and returning the upstream cursor as
        ``next_page_start`` so the caller can resume.

        With a non-blank ``name`` it routes to the cross-project
        ``GET /rest/api/1.0/repos`` search, always scoped to this project via the
        ``projectkey`` query param (never an unscoped all-repositories scan), with
        ``name`` as a server-side filter. Both forms return the same
        :class:`~mcp_atlassian.models.bitbucket.BitbucketRepository` model and the
        same single-window page shape. Because ``name`` selects the endpoint, it
        must be re-passed on every resume call — dropping it on a follow-up
        switches back to the project-scoped listing, so the prior
        ``next_page_start`` no longer applies.

        Args:
            project_key: The project key whose repositories to list (e.g.
                ``"PROJ"``).
            name: Optional repository-name filter, matched case-insensitively
                with surrounding whitespace ignored. When set, results come from
                the cross-project search scoped to this project — a slightly wider
                visibility surface than the unfiltered project listing.
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of repositories to return. Clamped to
                ``[1, MAX_REPOS_LIMIT]``.

        Returns:
            A :class:`BitbucketRepositoriesPage` carrying the collected
            repositories, whether the upstream list was fully consumed
            (``is_last_page``), whether repositories were omitted because a bound
            was hit (``truncated``), and the ``next_page_start`` resume cursor.

        Raises:
            ValueError: If ``project_key`` is blank, a page response is not a
                paged object with a ``values`` list, or the request fails (see
                :meth:`_get`).
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        key = project_key.strip()
        if not key:
            raise ValueError("project_key must be a non-empty Bitbucket project key.")
        limit = max(1, min(limit, MAX_REPOS_LIMIT))
        params: dict[str, Any] | None
        if name and name.strip():
            # Cross-project search, always scoped to this project: project_key
            # rides as a query param (requests URL-encodes it), never an
            # unscoped all-repositories scan.
            path = "/repos"
            params = {"projectkey": key, "name": name.strip()}
        else:
            # project_key is caller-supplied and goes into the request path, so
            # it is percent-encoded (no unescaped slashes) to prevent path
            # traversal or query-string injection into the Bitbucket request.
            path = f"/projects/{quote(key, safe='')}/repos"
            params = None
        # Single window: one upstream request, cursor surfaced for resumption.
        page = self._paginate(
            path,
            limit=limit,
            page_size=limit,
            max_pages=1,
            start=start,
            params=params,
        )
        repositories = [
            BitbucketRepository.from_api_response(value) for value in page.values
        ]
        return BitbucketRepositoriesPage(
            repositories=repositories,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )
