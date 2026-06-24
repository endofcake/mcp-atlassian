"""Bitbucket Data Center branch and tag (ref) operations."""

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..models.bitbucket import BitbucketBranch, BitbucketTag
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Default number of refs a single list_branches/list_tags call returns.
DEFAULT_REFS_LIMIT = 25
# Hard ceiling on refs returned in one call (matching the repos ceiling).
# Bitbucket DC paginates the branches/tags endpoints; this caps the per-call
# window (the tool's `limit`), and the caller pages further with the cursor.
MAX_REFS_LIMIT = 1000

# The two ``orderBy`` values both ref endpoints accept. The 9.4 tags endpoint
# documents no enum, so the value is normalized client-side before it is sent.
_REF_ORDER_BY = {"ALPHABETICAL", "MODIFICATION"}


@dataclass
class BitbucketBranchesPage:
    """A bounded, possibly-truncated view of a repository's branches.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage` for the full
    meaning of the ``is_last_page`` / ``truncated`` combinations).

    Attributes:
        branches: The collected branch models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``branches`` omits branches that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
    """

    branches: list[BitbucketBranch]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


@dataclass
class BitbucketTagsPage:
    """A bounded, possibly-truncated view of a repository's tags.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage`).

    Attributes:
        tags: The collected tag models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``tags`` omits tags that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
    """

    tags: list[BitbucketTag]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


class RefsMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center branch and tag operations."""

    @staticmethod
    def _refs_base_path(project_key: str, repository_slug: str, collection: str) -> str:
        """Validate and percent-encode a repo-scoped refs collection path.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            collection: The collection segment (``"branches"`` or ``"tags"``).

        Returns:
            ``/projects/{key}/repos/{slug}/{collection}`` with both caller
            segments percent-encoded (no unescaped slashes) to prevent path
            traversal.

        Raises:
            ValueError: If either caller segment is blank.
        """
        key = project_key.strip()
        slug = repository_slug.strip()
        if not key:
            raise ValueError("project_key must be a non-empty Bitbucket project key.")
        if not slug:
            raise ValueError(
                "repository_slug must be a non-empty Bitbucket repository slug."
            )
        return (
            f"/projects/{quote(key, safe='')}/repos/{quote(slug, safe='')}/{collection}"
        )

    @staticmethod
    def _ref_list_params(
        filter_text: str | None,
        order_by: str | None,
        boost_matches: bool | None = None,
    ) -> dict[str, Any]:
        """Build the query params shared by the branch and tag list endpoints.

        ``filter_text`` is a server-side name filter — never a client walk.
        ``order_by`` is normalized to the two known values (the 9.4 tags
        endpoint documents no enum), uppercased; an unrecognised value is
        dropped so the server default applies. ``boost_matches`` is
        branches-only — only :meth:`list_branches` passes it; the tags endpoint
        does not accept it, so it is left at ``None`` for tags.

        Args:
            filter_text: Optional server-side name filter.
            order_by: Optional ordering (``ALPHABETICAL`` or ``MODIFICATION``).
            boost_matches: Optional flag to boost exact/prefix filter matches
                (branches only).

        Returns:
            A params dict carrying only the supplied, valid values.
        """
        params: dict[str, Any] = {}
        if filter_text and filter_text.strip():
            params["filterText"] = filter_text.strip()
        if order_by is not None:
            normalized = order_by.strip().upper()
            if normalized in _REF_ORDER_BY:
                params["orderBy"] = normalized
        if boost_matches is not None:
            # boostMatches is a boolean param, but ``requests`` renders a raw
            # Python bool as the capitalized "True"/"False", which the Bitbucket
            # instance rejects; serialize the lowercase string explicitly.
            params["boostMatches"] = "true" if boost_matches else "false"
        return params

    def list_branches(
        self,
        project_key: str,
        repository_slug: str,
        *,
        filter_text: str | None = None,
        order_by: str | None = None,
        boost_matches: bool | None = None,
        start: int = 0,
        limit: int = DEFAULT_REFS_LIMIT,
    ) -> BitbucketBranchesPage:
        """List branches in a Bitbucket Data Center repository.

        Calls ``GET /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        branches`` (paged with ``start``/``limit``), fetching a single window
        (``start`` → up to ``limit``) in one request and returning the upstream
        cursor as ``next_page_start`` so the caller can resume. ``filter_text``
        is matched server-side — never a client walk.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            filter_text: Optional server-side branch-name filter.
            order_by: Optional ordering — ``ALPHABETICAL`` or ``MODIFICATION``;
                an unrecognised value falls back to the server default.
            boost_matches: When set, floats exact/prefix ``filter_text`` hits up.
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of branches to return. Clamped to
                ``[1, MAX_REFS_LIMIT]``.

        Returns:
            A :class:`BitbucketBranchesPage` carrying the collected branches,
            whether the upstream list was fully consumed (``is_last_page``),
            whether branches were omitted (``truncated``), and the
            ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, a page is misshaped, or the
                request fails (see :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path = self._refs_base_path(project_key, repository_slug, "branches")
        limit = max(1, min(limit, MAX_REFS_LIMIT))
        params = self._ref_list_params(filter_text, order_by, boost_matches)
        # Single window: one upstream request, cursor surfaced for resumption.
        page = self._paginate(
            path,
            limit=limit,
            page_size=limit,
            max_pages=1,
            start=start,
            params=params or None,
        )
        branches = [BitbucketBranch.from_api_response(value) for value in page.values]
        return BitbucketBranchesPage(
            branches=branches,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    def list_tags(
        self,
        project_key: str,
        repository_slug: str,
        *,
        filter_text: str | None = None,
        order_by: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_REFS_LIMIT,
    ) -> BitbucketTagsPage:
        """List tags in a Bitbucket Data Center repository.

        Calls ``GET /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        tags`` (paged with ``start``/``limit``), fetching a single window
        (``start`` → up to ``limit``) in one request and returning the upstream
        cursor as ``next_page_start`` so the caller can resume. ``filter_text``
        is matched server-side — never a client walk.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            filter_text: Optional server-side tag-name filter.
            order_by: Optional ordering — ``ALPHABETICAL`` or ``MODIFICATION``;
                an unrecognised value falls back to the server default.
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of tags to return. Clamped to
                ``[1, MAX_REFS_LIMIT]``.

        Returns:
            A :class:`BitbucketTagsPage` carrying the collected tags, whether the
            upstream list was fully consumed (``is_last_page``), whether tags
            were omitted (``truncated``), and the ``next_page_start`` resume
            cursor.

        Raises:
            ValueError: If a segment is blank, a page is misshaped, or the
                request fails (see :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path = self._refs_base_path(project_key, repository_slug, "tags")
        limit = max(1, min(limit, MAX_REFS_LIMIT))
        params = self._ref_list_params(filter_text, order_by)
        # Single window: one upstream request, cursor surfaced for resumption.
        page = self._paginate(
            path,
            limit=limit,
            page_size=limit,
            max_pages=1,
            start=start,
            params=params or None,
        )
        tags = [BitbucketTag.from_api_response(value) for value in page.values]
        return BitbucketTagsPage(
            tags=tags,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    def get_tag(
        self, project_key: str, repository_slug: str, name: str
    ) -> BitbucketTag:
        """Get a single tag in a Bitbucket Data Center repository.

        Calls ``GET .../tags/{name}``. The tag name is percent-encoded because
        tag names may carry slashes and other special characters.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            name: The tag name (e.g. ``"release/1.0"`` or ``"v1.0.0"``).

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketTag`.

        Raises:
            ValueError: If a segment is blank, the tag name is blank, the
                response is misshaped, or the request fails.
            BitbucketResourceNotFoundError: If the tag does not exist or is not
                accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._refs_base_path(project_key, repository_slug, "tags")
        tag_name = name.strip()
        if not tag_name:
            raise ValueError("name must be a non-empty Bitbucket tag name.")
        data = self._get(f"{base}/{quote(tag_name, safe='')}")
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{base}/{quote(tag_name, safe='')}; expected a tag object."
            )
        return BitbucketTag.from_api_response(data)

    def get_default_branch(
        self, project_key: str, repository_slug: str
    ) -> BitbucketBranch:
        """Get a repository's default branch.

        Calls ``GET .../default-branch``, which returns a ``RestMinimalRef``
        (``id`` / ``displayId`` / ``type`` only — no commit SHA), parsed into a
        :class:`~mcp_atlassian.models.bitbucket.BitbucketBranch`.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketBranch` with only
            the minimal-ref fields populated.

        Raises:
            ValueError: If a segment is blank, the response is misshaped, or the
                request fails.
            BitbucketResourceNotFoundError: If the repository does not exist, is
                not accessible, or has no default branch.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path = self._refs_base_path(project_key, repository_slug, "default-branch")
        data = self._get(path)
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a ref object."
            )
        return BitbucketBranch.from_api_response(data)
