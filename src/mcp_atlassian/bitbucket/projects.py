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
        self, *, start: int = 0, limit: int = DEFAULT_PROJECTS_LIMIT
    ) -> BitbucketProjectsPage:
        """List Bitbucket projects visible to the authenticated user.

        Bitbucket Data Center paginates the projects endpoint. Without a
        ``config.projects_filter`` this fetches a single window (``start`` → up
        to ``limit``) in one request and returns the upstream cursor as
        ``next_page_start`` so the caller can resume. When a ``projects_filter``
        is configured the allowlist is applied client-side, so each window is
        filled by a bounded server-side walk (capped by a hard page limit); the
        returned ``count`` is post-filter, so a window can be empty while more
        pages remain — keep paging with ``start=next_page_start`` until
        ``is_last_page``.

        Args:
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of projects to return. Clamped to
                ``[1, MAX_PROJECTS_LIMIT]``.

        Returns:
            A :class:`BitbucketProjectsPage` carrying the collected project
            models, whether the upstream list was fully consumed
            (``is_last_page``), whether projects were omitted because a bound was
            hit (``truncated``), and the ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a page response is not a paged object with a
                ``values`` list, or if the request fails (see :meth:`_get`).
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        limit = max(1, min(limit, MAX_PROJECTS_LIMIT))
        filter_keys = self._projects_filter_keys()

        def _filter(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [p for p in values if str(p.get("key", "")).upper() in filter_keys]

        # Filtered: a bounded server-side walk per window keeps the per-call
        # request cap. Unfiltered: a single window (one upstream request) with
        # the cursor surfaced for resumption.
        if filter_keys is not None:
            transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = (
                _filter
            )
            page_size = _PROJECTS_PAGE_SIZE
            max_pages = _MAX_PROJECT_PAGES
        else:
            transform = None
            page_size = limit
            max_pages = 1
        # Filtering runs on raw dicts mid-walk; model conversion happens once the
        # page is collected (parallel to ReposMixin.list_repositories).
        page = self._paginate(
            "/projects",
            limit=limit,
            page_size=page_size,
            max_pages=max_pages,
            start=start,
            transform=transform,
        )
        projects = [BitbucketProject.from_api_response(value) for value in page.values]
        return BitbucketProjectsPage(
            projects=projects,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )
