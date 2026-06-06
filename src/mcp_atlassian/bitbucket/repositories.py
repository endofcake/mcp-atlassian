"""Bitbucket Data Center repository operations."""

import logging
from dataclasses import dataclass
from urllib.parse import quote

from ..models.bitbucket import BitbucketRepository
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Default number of repositories a single list_repositories call returns.
DEFAULT_REPOS_LIMIT = 25
# Hard ceiling on repositories returned in one call. Bitbucket DC paginates, so
# an unbounded walk could fetch many pages; this caps the result and the tool's
# `limit` parameter, with truncation surfaced explicitly to the caller.
MAX_REPOS_LIMIT = 1000
# Per-request page size for the underlying paged endpoint.
_REPOS_PAGE_SIZE = 100
# Bound on the pagination loop. Reaching MAX_REPOS_LIMIT needs
# ceil(1000 / 100) = 10 full pages; the headroom tolerates short pages and
# bounds calls to a misbehaving instance. Repos are project-scoped (no
# client-side filter walk), so a smaller cap than projects suffices.
_MAX_REPO_PAGES = 15


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
    """

    repositories: list[BitbucketRepository]
    is_last_page: bool
    truncated: bool


class ReposMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center repository operations."""

    def list_repositories(
        self, project_key: str, limit: int = DEFAULT_REPOS_LIMIT
    ) -> BitbucketRepositoriesPage:
        """List repositories in a Bitbucket Data Center project.

        Calls ``GET /rest/api/1.0/projects/{projectKey}/repos`` (paged with
        ``start``/``limit``), following pages until the project's repository list
        is exhausted, ``limit`` is reached, or the page cap is hit.

        Args:
            project_key: The project key whose repositories to list (e.g.
                ``"PROJ"``).
            limit: Maximum number of repositories to return. Clamped to
                ``[1, MAX_REPOS_LIMIT]``.

        Returns:
            A :class:`BitbucketRepositoriesPage` carrying the collected
            repositories, whether the upstream list was fully consumed
            (``is_last_page``), and whether repositories were omitted because a
            bound was hit (``truncated``).

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
        # project_key is caller-supplied and goes into the request path, so it is
        # percent-encoded (no unescaped slashes) to prevent path traversal or
        # query-string injection into the Bitbucket request.
        path = f"/projects/{quote(key, safe='')}/repos"
        page = self._paginate(
            path,
            limit=limit,
            page_size=_REPOS_PAGE_SIZE,
            max_pages=_MAX_REPO_PAGES,
        )
        repositories = [
            BitbucketRepository.from_api_response(value) for value in page.values
        ]
        return BitbucketRepositoriesPage(
            repositories=repositories,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
        )
