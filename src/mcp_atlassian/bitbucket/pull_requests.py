"""Bitbucket Data Center pull-request operations."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..models.bitbucket import (
    BitbucketActivity,
    BitbucketPullRequest,
    BitbucketPullRequestDiff,
)
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# --- list_pull_requests bounds ---
# Default number of pull requests a single list_pull_requests call returns.
DEFAULT_PRS_LIMIT = 25
# Hard ceiling on pull requests returned in one call.
MAX_PRS_LIMIT = 100
# Per-request page size for the underlying paged endpoint.
_PRS_PAGE_SIZE = 50
# Bound on the pagination loop. state/direction/at/order are server-side query
# params (no client-side filter walk), so reaching MAX_PRS_LIMIT needs
# ceil(100 / 50) = 2 pages; the headroom tolerates short pages.
_MAX_PR_PAGES = 10

# --- get_activities bounds ---
# Default number of activity entries a single get_activities call returns.
DEFAULT_ACTIVITIES_LIMIT = 25
# Hard ceiling on activity entries returned in one call.
MAX_ACTIVITIES_LIMIT = 200
# Per-request page size for the underlying paged endpoint.
_ACTIVITIES_PAGE_SIZE = 50
# Bound on the pagination loop. The activities endpoint has no action query
# filter, so an action filter (e.g. comments-only) is applied client-side and
# may scan many pages before collecting `limit` matches: 50 * 50 = 2500 entries.
_MAX_ACTIVITY_PAGES = 50

# --- get_pull_request_diff bounds ---
# Default per-file diff-line cap (token control). The diff is one payload, so the
# bound is on lines-per-file and total files, not pagination.
DEFAULT_MAX_LINES_PER_FILE = 500
# Hard ceiling on the per-file diff-line cap the caller may request.
MAX_MAX_LINES_PER_FILE = 5000
# Default and ceiling on the number of file diffs retained in one call.
DEFAULT_MAX_FILES = 100
MAX_MAX_FILES = 1000


@dataclass
class BitbucketPullRequestsPage:
    """A bounded, possibly-truncated view of a repository's pull requests.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage` for the full
    meaning of the ``is_last_page`` / ``truncated`` combinations).

    Attributes:
        pull_requests: The collected pull-request models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``pull_requests`` omits PRs that exist.
    """

    pull_requests: list[BitbucketPullRequest]
    is_last_page: bool
    truncated: bool


@dataclass
class BitbucketActivitiesPage:
    """A bounded, possibly-truncated view of a pull request's activity timeline.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage`). When an
    ``action`` filter is applied, an empty ``truncated`` result means the scan
    did not reach a matching entry, not that none exists.

    Attributes:
        activities: The collected activity models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``activities`` omits entries that exist.
    """

    activities: list[BitbucketActivity]
    is_last_page: bool
    truncated: bool


class PullRequestsMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center pull-request operations."""

    @staticmethod
    def _pr_base_path(project_key: str, repository_slug: str) -> str:
        """Validate and percent-encode the PR collection path for a repository.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).

        Returns:
            ``/projects/{key}/repos/{slug}/pull-requests`` with both caller
            segments percent-encoded (no unescaped slashes) to prevent path
            traversal or query-string injection.

        Raises:
            ValueError: If either segment is blank.
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
            f"/projects/{quote(key, safe='')}"
            f"/repos/{quote(slug, safe='')}/pull-requests"
        )

    @staticmethod
    def _coerce_pr_id(pull_request_id: int | str) -> int:
        """Coerce and validate a pull-request id to a positive integer.

        Args:
            pull_request_id: The caller-supplied pull-request id.

        Returns:
            The id as a positive ``int`` (safe to interpolate into the path —
            an integer cannot carry traversal or injection).

        Raises:
            ValueError: If the id is not a positive integer.
        """
        try:
            pr_id = int(pull_request_id)
        except (TypeError, ValueError):
            raise ValueError("pull_request_id must be a positive integer.") from None
        if pr_id <= 0:
            raise ValueError("pull_request_id must be a positive integer.")
        return pr_id

    def list_pull_requests(
        self,
        project_key: str,
        repository_slug: str,
        *,
        state: str | None = None,
        direction: str | None = None,
        at: str | None = None,
        order: str | None = None,
        limit: int = DEFAULT_PRS_LIMIT,
    ) -> BitbucketPullRequestsPage:
        """List pull requests in a Bitbucket Data Center repository.

        Calls ``GET /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        pull-requests`` (paged with ``start``/``limit``), following pages until
        the list is exhausted, ``limit`` is reached, or the page cap is hit.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            state: Optional state filter — ``OPEN`` (default upstream),
                ``DECLINED``, ``MERGED``, or ``ALL``.
            direction: Optional direction relative to the repository —
                ``INCOMING`` (default upstream) or ``OUTGOING``.
            at: Optional fully-qualified branch ref to filter on (e.g.
                ``refs/heads/main``).
            order: Optional ordering — ``NEWEST`` (default upstream) or
                ``OLDEST``.
            limit: Maximum number of pull requests to return. Clamped to
                ``[1, MAX_PRS_LIMIT]``.

        Returns:
            A :class:`BitbucketPullRequestsPage` carrying the collected pull
            requests, whether the upstream list was fully consumed
            (``is_last_page``), and whether PRs were omitted (``truncated``).

        Raises:
            ValueError: If a segment is blank, a page is misshaped, or the
                request fails (see :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path = self._pr_base_path(project_key, repository_slug)
        limit = max(1, min(limit, MAX_PRS_LIMIT))
        params: dict[str, Any] = {}
        if state is not None:
            params["state"] = state
        if direction is not None:
            params["direction"] = direction
        if at is not None:
            params["at"] = at
        if order is not None:
            params["order"] = order
        page = self._paginate(
            path,
            limit=limit,
            page_size=_PRS_PAGE_SIZE,
            max_pages=_MAX_PR_PAGES,
            params=params or None,
        )
        pull_requests = [
            BitbucketPullRequest.from_api_response(value) for value in page.values
        ]
        return BitbucketPullRequestsPage(
            pull_requests=pull_requests,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
        )

    def get_pull_request(
        self, project_key: str, repository_slug: str, pull_request_id: int | str
    ) -> BitbucketPullRequest:
        """Get a single pull request's metadata and reviewer/approval state.

        Calls ``GET .../pull-requests/{pullRequestId}``.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                the response is misshaped, or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        data = self._get(f"{base}/{pr_id}")
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{base}/{pr_id}; expected a pull-request object."
            )
        return BitbucketPullRequest.from_api_response(data)

    def get_pull_request_diff(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        *,
        max_lines_per_file: int = DEFAULT_MAX_LINES_PER_FILE,
        max_files: int = DEFAULT_MAX_FILES,
        context_lines: int | None = None,
    ) -> BitbucketPullRequestDiff:
        """Get a pull request's structured diff, bounded for token control.

        Calls the whole-PR diff form ``GET .../pull-requests/{pullRequestId}/
        diff``. The spec documents ``/diff/{path}`` with a required ``path``;
        Bitbucket serves the path-less form as the full structured diff. The
        response is one payload — a ``RestDiffResponse`` whose ``diffs`` array
        holds one structured ``RestDiff`` per file — so it is fetched with a
        single request (not walked).
        ``withComments`` is forced off to keep inline comments out of the diff
        payload. The per-file line budget and file cap are applied at model-build
        time and surfaced via the diff's ``truncated`` flags.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            max_lines_per_file: Per-file diff-line cap. Clamped to
                ``[1, MAX_MAX_LINES_PER_FILE]``.
            max_files: Maximum number of file diffs to retain. Clamped to
                ``[1, MAX_MAX_FILES]``.
            context_lines: Optional number of context lines around changes; when
                None the server default is used.

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequestDiff`.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                the response is not a JSON object, or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        max_lines_per_file = max(1, min(max_lines_per_file, MAX_MAX_LINES_PER_FILE))
        max_files = max(1, min(max_files, MAX_MAX_FILES))
        params: dict[str, Any] = {"withComments": "false"}
        if context_lines is not None:
            params["contextLines"] = context_lines
        data = self._get(f"{base}/{pr_id}/diff", params=params)
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{base}/{pr_id}/diff; expected a diff object."
            )
        return BitbucketPullRequestDiff.from_api_response(
            data, max_lines_per_file=max_lines_per_file, max_files=max_files
        )

    def get_activities(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        *,
        action: str | None = None,
        limit: int = DEFAULT_ACTIVITIES_LIMIT,
    ) -> BitbucketActivitiesPage:
        """List a pull request's activity timeline (comments + approvals + …).

        Calls ``GET .../pull-requests/{pullRequestId}/activities`` (paged). The
        endpoint exposes no action query filter, so ``action`` is applied
        client-side during the walk (the same mechanism as the projects filter),
        which is how the comments view is built (``action="COMMENTED"``).

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            action: Optional activity action to filter to (e.g. ``"COMMENTED"``),
                matched case-insensitively.
            limit: Maximum number of activity entries to return. Clamped to
                ``[1, MAX_ACTIVITIES_LIMIT]``.

        Returns:
            A :class:`BitbucketActivitiesPage` carrying the collected activities,
            whether the upstream list was fully consumed (``is_last_page``), and
            whether entries were omitted (``truncated``).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                a page is misshaped, or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        limit = max(1, min(limit, MAX_ACTIVITIES_LIMIT))

        transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
        if action is not None:
            wanted = action.strip().upper()

            def _filter(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [v for v in values if str(v.get("action", "")).upper() == wanted]

            transform = _filter

        page = self._paginate(
            f"{base}/{pr_id}/activities",
            limit=limit,
            page_size=_ACTIVITIES_PAGE_SIZE,
            max_pages=_MAX_ACTIVITY_PAGES,
            transform=transform,
        )
        activities = [
            BitbucketActivity.from_api_response(value) for value in page.values
        ]
        return BitbucketActivitiesPage(
            activities=activities,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
        )
