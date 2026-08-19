"""Bitbucket Data Center project operations."""

import logging
from collections.abc import Callable
from typing import Any

from ..models.bitbucket import BitbucketProject
from ..utils.pagination import clamp_limit
from .client import (
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

        This filter intentionally scopes discovery/listing only, mirroring
        JIRA_PROJECTS_FILTER and CONFLUENCE_SPACES_FILTER: it is not an
        authorization boundary, and tools invoked with an explicit project key
        are limited only by the user's own Bitbucket permissions.

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
        self,
        *,
        name: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_PROJECTS_LIMIT,
    ) -> BitbucketProjectsPage:
        """List Bitbucket projects visible to the authenticated user.

        Bitbucket Data Center paginates the projects endpoint; one window per
        call, resumable via the returned ``next_page_start``.
        When a ``config.projects_filter`` is configured the allowlist narrows the
        window client-side, so the returned projects are post-filter: a window
        can be empty while more pages remain. Keep paging with
        ``start=next_page_start`` until ``is_last_page``.

        A non-blank ``name`` is passed through as a server-side query filter in
        both modes; it is orthogonal to the ``projects_filter`` key allowlist, so
        the two narrow the result independently.

        Args:
            name: Optional server-side filter on project name. The match
                semantics are decided by the instance.
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
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.list_projects"),
                MAX_PROJECTS_LIMIT,
            ),
        )
        filter_keys = self._projects_filter_keys()
        # A server-side name filter rides on whichever mode is selected below; it
        # is independent of the client-side key allowlist.
        params: dict[str, Any] | None = (
            {"name": name.strip()} if name and name.strip() else None
        )

        def _filter(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [p for p in values if str(p.get("key", "")).upper() in filter_keys]

        # An allowlist narrows the fetched window client-side; the raw upstream
        # cursor is returned even when the narrowed window is empty.
        transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = (
            _filter if filter_keys is not None else None
        )
        page = self._fetch_page(
            "/projects",
            limit=limit,
            start=start,
            params=params,
            transform=transform,
        )
        projects = [BitbucketProject.from_api_response(value) for value in page.values]
        return BitbucketProjectsPage(
            projects=projects,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )
