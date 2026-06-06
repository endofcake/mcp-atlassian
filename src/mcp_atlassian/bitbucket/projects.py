"""Bitbucket Data Center project operations."""

import logging
from collections.abc import Callable
from typing import Any

from ..models.bitbucket import BitbucketProject
from .client import (
    _MAX_PROJECT_PAGES,
    _PROJECTS_PAGE_SIZE,
    DEFAULT_PROJECTS_LIMIT,
    MAX_PROJECTS_LIMIT,
    BitbucketClient,
    BitbucketProjectsPage,
)

logger = logging.getLogger("mcp-atlassian.bitbucket")


class ProjectsMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center project operations."""

    def _projects_filter_keys(self) -> set[str] | None:
        """Parse ``projects_filter`` into an allowlist of upper-cased keys.

        Bitbucket has no list-level key filter, so the configured allowlist is
        applied client-side. Keys are upper-cased on both sides because Bitbucket
        Data Center project keys are canonically upper case, so a lower-case
        filter entry should still match.

        Returns:
            The set of allowed project keys (upper-cased), or ``None`` when no
            filter is configured.
        """
        raw = self.config.projects_filter
        if not raw:
            return None
        keys = {key.strip().upper() for key in raw.split(",") if key.strip()}
        return keys or None

    def list_projects(
        self, limit: int = DEFAULT_PROJECTS_LIMIT
    ) -> BitbucketProjectsPage:
        """List Bitbucket projects visible to the authenticated user.

        Bitbucket Data Center paginates the projects endpoint, so a single page
        can silently omit projects. This walks pages (``start``/``nextPageStart``)
        bounded by ``limit`` and a hard safety cap, and reports whether the
        result is complete. When ``config.projects_filter`` is set, each page is
        narrowed to that allowlist before the bound is applied.

        Args:
            limit: Maximum number of projects to return. Clamped to
                ``[1, MAX_PROJECTS_LIMIT]``.

        Returns:
            A :class:`BitbucketProjectsPage` carrying the collected project
            models, whether the upstream list was fully consumed
            (``is_last_page``), and whether projects were omitted because a bound
            was hit (``truncated``).

        Raises:
            ValueError: If a page response is not a paged object with a
                ``values`` list, or if the request fails (see :meth:`_get`).
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        limit = max(1, min(limit, MAX_PROJECTS_LIMIT))
        filter_keys = self._projects_filter_keys()

        def _filter(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [p for p in values if str(p.get("key", "")).upper() in filter_keys]

        transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = (
            _filter if filter_keys is not None else None
        )
        # Filtering runs on raw dicts mid-walk; model conversion happens once the
        # page is collected (parallel to ReposMixin.list_repositories).
        page = self._paginate(
            "/projects",
            limit=limit,
            page_size=_PROJECTS_PAGE_SIZE,
            max_pages=_MAX_PROJECT_PAGES,
            transform=transform,
        )
        projects = [BitbucketProject.from_api_response(value) for value in page.values]
        return BitbucketProjectsPage(
            projects=projects,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
        )
