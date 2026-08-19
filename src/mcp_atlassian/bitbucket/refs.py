"""Bitbucket Data Center branch and tag (ref) operations."""

import logging
from dataclasses import dataclass
from typing import Any

from ..models.bitbucket import BitbucketBranch, BitbucketTag
from ..utils.pagination import clamp_limit
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Default number of refs a single list_branches/list_tags call returns.
DEFAULT_REFS_LIMIT = 25
# Hard ceiling on refs returned in one call (matching the repos ceiling).
# Bitbucket DC paginates the branches/tags endpoints; this caps the per-call
# window (the tool's `limit`), and the caller pages further with the cursor.
MAX_REFS_LIMIT = 1000

# The two ``orderBy`` values both ref endpoints accept (the tags endpoint
# documents no enum); normalized through BitbucketClient._enum_param (a blank
# value is dropped, any other value raises).
_REF_ORDER_BY = ("ALPHABETICAL", "MODIFICATION")


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
    def _ref_list_params(
        filter_text: str | None,
        order_by: str | None,
        boost_matches: bool | None = None,
    ) -> dict[str, Any]:
        """Build the query params shared by the branch and tag list endpoints.

        ``filter_text`` is a server-side name filter. ``order_by`` is
        normalized to the two known values (the tags endpoint documents no
        enum); a blank value is dropped and an unrecognised value raises (see
        :meth:`BitbucketClient._enum_param`). The tags endpoint does not
        accept ``boost_matches``, so only :meth:`list_branches` passes it.

        Args:
            filter_text: Optional server-side name filter.
            order_by: Optional ordering (``ALPHABETICAL`` or ``MODIFICATION``).
            boost_matches: Optional flag to boost exact/prefix filter matches
                (branches only).

        Returns:
            A params dict carrying only the supplied, valid values.

        Raises:
            ValueError: If ``order_by`` is not a recognised value.
        """
        params: dict[str, Any] = {}
        if filter_text and filter_text.strip():
            params["filterText"] = filter_text.strip()
        order = BitbucketClient._enum_param(
            order_by, name="order_by", allowed=_REF_ORDER_BY
        )
        if order is not None:
            params["orderBy"] = order
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
        branches`` (paged with ``start``/``limit``); one window per
        call, resumable via the returned ``next_page_start``. ``filter_text``
        is matched server-side.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            filter_text: Optional server-side branch-name filter.
            order_by: Optional ordering (``ALPHABETICAL`` or ``MODIFICATION``).
                A blank value applies the server default; an unrecognised
                value raises before any request is issued.
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
            ValueError: If a segment is blank, ``order_by`` is not a recognised
                value, a page is misshaped, or the request fails (see
                :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path = f"{self._repo_base_path(project_key, repository_slug)}/branches"
        limit = max(
            1,
            min(clamp_limit(limit, context="bitbucket.list_branches"), MAX_REFS_LIMIT),
        )
        params = self._ref_list_params(filter_text, order_by, boost_matches)
        page = self._fetch_page(
            path,
            limit=limit,
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
        tags`` (paged with ``start``/``limit``); one window per
        call, resumable via the returned ``next_page_start``. ``filter_text``
        is matched server-side.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            filter_text: Optional server-side tag-name filter.
            order_by: Optional ordering (``ALPHABETICAL`` or ``MODIFICATION``).
                A blank value applies the server default; an unrecognised
                value raises before any request is issued.
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
            ValueError: If a segment is blank, ``order_by`` is not a recognised
                value, a page is misshaped, or the request fails (see
                :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path = f"{self._repo_base_path(project_key, repository_slug)}/tags"
        limit = max(
            1, min(clamp_limit(limit, context="bitbucket.list_tags"), MAX_REFS_LIMIT)
        )
        params = self._ref_list_params(filter_text, order_by)
        page = self._fetch_page(
            path,
            limit=limit,
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

        Calls ``GET .../tags/{name}``. The tag name is encoded one path
        component at a time (see :meth:`BitbucketClient._encode_repo_path`):
        a slash in a name such as ``release/1.0`` stays a literal path
        separator, which the ``{name:.*}`` path parameter accepts, while each
        component is percent-encoded.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            name: The tag name (e.g. ``"release/1.0"`` or ``"v1.0.0"``).

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketTag`.

        Raises:
            ValueError: If a segment is blank, the tag name is blank or has an
                empty, ``.``, or ``..`` component, the response is misshaped,
                or the request fails.
            BitbucketResourceNotFoundError: If the tag does not exist or is not
                accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = f"{self._repo_base_path(project_key, repository_slug)}/tags"
        encoded_name = self._encode_repo_path(name.strip(), what="tag name")
        if not encoded_name:
            raise ValueError("name must be a non-empty Bitbucket tag name.")
        path = f"{base}/{encoded_name}"
        data = self._get(path)
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a tag object."
            )
        return BitbucketTag.from_api_response(data)

    def get_default_branch(
        self, project_key: str, repository_slug: str
    ) -> BitbucketBranch:
        """Get a repository's default branch.

        Calls ``GET .../default-branch``, which returns a ``RestMinimalRef``
        (``id``, ``displayId``, and ``type``, with no commit SHA), parsed into a
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
        path = f"{self._repo_base_path(project_key, repository_slug)}/default-branch"
        data = self._get(path)
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a ref object."
            )
        return BitbucketBranch.from_api_response(data)
