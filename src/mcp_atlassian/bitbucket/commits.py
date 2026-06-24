"""Bitbucket Data Center commit operations."""

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..models.bitbucket import BitbucketCommit
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Default number of commits a single list call returns.
DEFAULT_COMMITS_LIMIT = 25
# Hard ceiling on commits returned in one call (matching the refs ceiling).
# Bitbucket DC paginates the commits endpoints; this caps the per-call window
# (the tool's `limit`), and the caller pages further with the cursor.
MAX_COMMITS_LIMIT = 1000

# The three values the ``merges`` query param accepts. An unrecognised value is
# dropped client-side so the server default applies (mirroring refs `orderBy`).
_MERGES_VALUES = {"exclude", "include", "only"}


@dataclass
class BitbucketCommitsPage:
    """A bounded, possibly-truncated view of a repository's commits.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage` for the full
    meaning of the ``is_last_page`` / ``truncated`` combinations).

    Attributes:
        commits: The collected commit models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``commits`` omits commits that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
    """

    commits: list[BitbucketCommit]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


class CommitsMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center commit operations."""

    @staticmethod
    def _commit_list_params(
        since: str | None,
        until: str | None,
        path: str | None,
        merges: str | None,
        follow_renames: bool | None,
        ignore_missing: bool | None,
    ) -> dict[str, Any]:
        """Build the query params for the commit-history endpoint.

        Every filter is applied server-side — never a client walk. ``merges`` is
        normalized to the three known values, lowercased; an unrecognised value
        is dropped so the server default applies. The string-typed bool params
        (``followRenames``, ``ignoreMissing``) are sent as the lowercase strings
        the instance expects, because ``requests`` renders a raw Python bool as
        the capitalized ``"True"``/``"False"`` the instance rejects.

        Args:
            since: Optional exclusive lower-bound commit/ref to start from.
            until: Optional inclusive upper-bound commit/ref (the branch tip).
            path: Optional path to restrict history to. ``follow_renames`` is
                only valid alongside a single-file ``path``.
            merges: Optional merge-commit handling — ``exclude``/``include``/
                ``only``; an unrecognised value falls back to the server default.
            follow_renames: When set, follow a file's history across renames
                (valid only with a single-file ``path``).
            ignore_missing: When set, ignore missing commits rather than failing.

        Returns:
            A params dict carrying only the supplied, valid values.

        Raises:
            ValueError: If ``follow_renames`` is set without a single-file
                ``path`` — the instance would reject this with an opaque 400, so
                it is caught client-side before any request.
        """
        if follow_renames is not None and not (path and path.strip()):
            raise ValueError("follow_renames requires a single-file 'path'.")
        params: dict[str, Any] = {}
        if since and since.strip():
            params["since"] = since.strip()
        if until and until.strip():
            params["until"] = until.strip()
        if path and path.strip():
            params["path"] = path.strip()
        if merges is not None:
            normalized = merges.strip().lower()
            if normalized in _MERGES_VALUES:
                params["merges"] = normalized
        if follow_renames is not None:
            params["followRenames"] = "true" if follow_renames else "false"
        if ignore_missing is not None:
            params["ignoreMissing"] = "true" if ignore_missing else "false"
        return params

    def list_commits(
        self,
        project_key: str,
        repository_slug: str,
        *,
        since: str | None = None,
        until: str | None = None,
        path: str | None = None,
        merges: str | None = None,
        follow_renames: bool | None = None,
        ignore_missing: bool | None = None,
        start: int = 0,
        limit: int = DEFAULT_COMMITS_LIMIT,
    ) -> BitbucketCommitsPage:
        """List commits in a Bitbucket Data Center repository.

        Calls ``GET /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        commits`` (paged with ``start``/``limit``), fetching a single window
        (``start`` → up to ``limit``) in one request and returning the upstream
        cursor as ``next_page_start`` so the caller can resume. The history
        filters (``since``/``until``/``path``/``merges``/``follow_renames``) are
        applied server-side — never a client walk.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            since: Optional exclusive lower-bound commit/ref to start from.
            until: Optional inclusive upper-bound commit/ref (e.g. a branch tip).
            path: Optional path to restrict history to.
            merges: Optional merge-commit handling — ``exclude``/``include``/
                ``only``; an unrecognised value falls back to the server default.
            follow_renames: When set, follow a file's history across renames
                (valid only with a single-file ``path``).
            ignore_missing: When set, ignore missing commits rather than failing.
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of commits to return. Clamped to
                ``[1, MAX_COMMITS_LIMIT]``.

        Returns:
            A :class:`BitbucketCommitsPage` carrying the collected commits,
            whether the upstream list was fully consumed (``is_last_page``),
            whether commits were omitted (``truncated``), and the
            ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, a page is misshaped, or the
                request fails (see :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path_base = f"{self._repo_base_path(project_key, repository_slug)}/commits"
        limit = max(1, min(limit, MAX_COMMITS_LIMIT))
        params = self._commit_list_params(
            since, until, path, merges, follow_renames, ignore_missing
        )
        # Single window: one upstream request, cursor surfaced for resumption.
        page = self._paginate(
            path_base,
            limit=limit,
            page_size=limit,
            max_pages=1,
            start=start,
            params=params or None,
        )
        commits = [BitbucketCommit.from_api_response(value) for value in page.values]
        return BitbucketCommitsPage(
            commits=commits,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    def get_commit(
        self, project_key: str, repository_slug: str, commit_id: str
    ) -> BitbucketCommit:
        """Get a single commit by id in a Bitbucket Data Center repository.

        Calls ``GET .../commits/{commitId}``. The ``path`` query param is
        deliberately omitted: with ``path`` set, the endpoint returns the first
        commit affecting the path at/before ``commitId`` rather than the commit
        itself, so omitting it keeps this a strict get-by-id. The commit id is
        percent-encoded.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            commit_id: The full or abbreviated commit SHA.

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketCommit`.

        Raises:
            ValueError: If a segment is blank, the commit id is blank, the
                response is misshaped, or the request fails.
            BitbucketResourceNotFoundError: If the commit does not exist or is
                not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = f"{self._repo_base_path(project_key, repository_slug)}/commits"
        sha = commit_id.strip()
        if not sha:
            raise ValueError("commit_id must be a non-empty Bitbucket commit id.")
        path = f"{base}/{quote(sha, safe='')}"
        data = self._get(path)
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a commit object."
            )
        return BitbucketCommit.from_api_response(data)

    def get_pull_request_commits(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        *,
        start: int = 0,
        limit: int = DEFAULT_COMMITS_LIMIT,
    ) -> BitbucketCommitsPage:
        """List the commits that make up a Bitbucket Data Center pull request.

        Calls ``GET .../pull-requests/{pullRequestId}/commits`` (paged with
        ``start``/``limit``), fetching a single window in one request and
        returning the upstream cursor as ``next_page_start`` so the caller can
        resume. This endpoint accepts only ``start``/``limit`` (no filters).

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            pull_request_id: The pull-request id (positive integer).
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of commits to return. Clamped to
                ``[1, MAX_COMMITS_LIMIT]``.

        Returns:
            A :class:`BitbucketCommitsPage` carrying the collected commits,
            whether the upstream list was fully consumed (``is_last_page``),
            whether commits were omitted (``truncated``), and the
            ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                a page is misshaped, or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._repo_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        path = f"{base}/pull-requests/{pr_id}/commits"
        limit = max(1, min(limit, MAX_COMMITS_LIMIT))
        # Single window: one upstream request, cursor surfaced for resumption.
        page = self._paginate(
            path,
            limit=limit,
            page_size=limit,
            max_pages=1,
            start=start,
        )
        commits = [BitbucketCommit.from_api_response(value) for value in page.values]
        return BitbucketCommitsPage(
            commits=commits,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )
