"""Bitbucket Data Center build-status operations.

These live under the ``/rest/build-status/1.0`` module rather than the core
``/rest/api/1.0`` API, so every request here passes ``BUILD_STATUS_BASE_PATH``
through the base client's ``base_path`` keyword.

The commit-scoped list endpoint is marked deprecated since Bitbucket 7.14 in
favour of the repository-scoped ``.../commits/{commitId}/builds`` resource.
It is still served in 9.4 and remains the only endpoint that lists every
status on a commit: the repository-scoped resource requires a build ``key``
and returns a single status. A future release may remove it, in which case
the instance answers 404 for this module and the mixin raises a
:class:`~mcp_atlassian.bitbucket.client.BitbucketResourceNotFoundError`
that says so.

The lookup is keyed on the commit id alone and is not repository scoped, so
a status may name a repository the caller cannot otherwise browse. That is
the endpoint's own permission model, which this server does not widen.
"""

import logging
import re
from dataclasses import dataclass, field

from ..models.bitbucket import BitbucketBuildStatus
from ..utils.pagination import clamp_limit
from .client import BitbucketClient, BitbucketResourceNotFoundError

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Bitbucket Data Center build-status REST module base path.
BUILD_STATUS_BASE_PATH = "/rest/build-status/1.0"

# Default number of build statuses a single call returns.
DEFAULT_BUILD_STATUSES_LIMIT = 25
# Hard ceiling on build statuses returned in one call. The endpoint itself
# serves at most the 100 most recent statuses on a commit, so a larger window
# cannot return more.
MAX_BUILD_STATUSES_LIMIT = 100

# The ``orderBy`` values the endpoint accepts; normalized through
# BitbucketClient._enum_param (a blank value is dropped, any other raises).
_BUILD_STATUS_ORDER_BY = ("NEWEST", "OLDEST", "STATUS")

# The endpoint documents the commit id as a full SHA-1 and is not repository
# scoped, so the instance has no repository in which to resolve an
# abbreviated id. A short id would most likely return an empty page that
# reads as "no builds", so only the full 40-character form is accepted.
_COMMIT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass
class BitbucketBuildStatusesPage:
    """A bounded, possibly-truncated view of a commit's build statuses.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage` for the full
    meaning of the ``is_last_page`` / ``truncated`` combinations).

    Attributes:
        statuses: The collected build-status models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``statuses`` omits statuses that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
        page_counts: The number of statuses in **this window** per state,
            keyed by the ``state`` value as the instance sent it. A status
            with no ``state`` is tallied under ``UNKNOWN``. Derived
            client-side from ``statuses``, so when ``truncated`` is true the
            counts do not cover the whole commit. A resumed page (``start``
            above 0) counts only its own window even when it is the last one.
    """

    statuses: list[BitbucketBuildStatus]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None
    page_counts: dict[str, int] = field(default_factory=dict)


class BuildsMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center build-status operations."""

    @staticmethod
    def _commit_id_segment(commit_id: str) -> str:
        """Validate a commit id as a full hex SHA and return it as a segment.

        A hex-only id carries no path or query separator, so it needs no
        percent-encoding. It is lower-cased, the form Bitbucket stores.

        Args:
            commit_id: The caller-supplied commit id.

        Returns:
            The stripped, lower-cased commit id.

        Raises:
            ValueError: If the id is not 40 hex characters. Raised before any
                request is issued.
        """
        text = (commit_id or "").strip()
        if not _COMMIT_ID_PATTERN.fullmatch(text):
            raise ValueError(
                "commit_id must be a full commit SHA of 40 hexadecimal characters."
            )
        return text.lower()

    def get_commit_build_statuses(
        self,
        commit_id: str,
        *,
        order_by: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_BUILD_STATUSES_LIMIT,
    ) -> BitbucketBuildStatusesPage:
        """List the CI build statuses posted against a commit.

        Calls ``GET /rest/build-status/1.0/commits/{commitId}`` (paged with
        ``start``/``limit``); one window per call, resumable via the returned
        ``next_page_start``. The endpoint is instance-wide (not repository
        scoped) and serves at most the 100 most recent statuses on a commit,
        so ``OLDEST`` orders within that window. An empty page means no
        status was posted against that exact id. It does not confirm the
        commit exists. The endpoint is deprecated upstream since Bitbucket
        7.14 but is the only list-shaped build-status read (see the module
        docstring).

        Args:
            commit_id: The full 40-character commit SHA.
            order_by: Optional ordering (``NEWEST``, ``OLDEST``, or
                ``STATUS``). A blank value applies the server default. An
                unrecognised value raises before any request is issued.
            start: The offset to resume from (the ``next_page_start`` of a
                prior call). 0 starts from the beginning.
            limit: Maximum number of statuses to return. Clamped to
                ``[1, MAX_BUILD_STATUSES_LIMIT]``.

        Returns:
            A :class:`BitbucketBuildStatusesPage` carrying the collected
            statuses, whether the upstream list was fully consumed
            (``is_last_page``), whether statuses were omitted (``truncated``),
            the ``next_page_start`` resume cursor, and the per-state
            ``page_counts`` for this window.

        Raises:
            ValueError: If ``commit_id`` is not a hex SHA, ``order_by`` is not
                a recognised value, a page is misshaped, or the request fails
                (see :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the build-status module is not
                served by the instance (absent, or removed in a later
                Bitbucket release). This endpoint does not answer 404 for an
                unknown commit id.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        sha = self._commit_id_segment(commit_id)
        path = f"/commits/{sha}"
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.get_commit_build_statuses"),
                MAX_BUILD_STATUSES_LIMIT,
            ),
        )
        params: dict[str, str] = {}
        order = self._enum_param(
            order_by, name="order_by", allowed=_BUILD_STATUS_ORDER_BY
        )
        if order is not None:
            params["orderBy"] = order
        try:
            page = self._fetch_page(
                path,
                limit=limit,
                start=start,
                params=params or None,
                base_path=BUILD_STATUS_BASE_PATH,
            )
        except BitbucketResourceNotFoundError as e:
            # The shared 404 text names the entity. Here a 404 means the
            # build-status module itself is not served. The upstream body is
            # not echoed.
            error_msg = (
                "Bitbucket build-status module not found (HTTP 404) for "
                f"{BUILD_STATUS_BASE_PATH}{path}. The instance does not serve "
                "the build-status REST module, or this Bitbucket release has "
                "removed the commit build-status endpoint (deprecated since "
                "7.14). An unknown commit id returns an empty page, not 404."
            )
            logger.error(error_msg)
            raise BitbucketResourceNotFoundError(error_msg) from e
        statuses = [
            BitbucketBuildStatus.from_api_response(value) for value in page.values
        ]
        page_counts: dict[str, int] = {}
        for status in statuses:
            state = status.state or "UNKNOWN"
            page_counts[state] = page_counts.get(state, 0) + 1
        return BitbucketBuildStatusesPage(
            statuses=statuses,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
            page_counts=page_counts,
        )
