"""Bitbucket Data Center pull-request operations."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..models.bitbucket import (
    BitbucketActivity,
    BitbucketChange,
    BitbucketComment,
    BitbucketFileDiff,
    BitbucketMergeStatus,
    BitbucketPullRequest,
    BitbucketPullRequestDiff,
    BitbucketUser,
)
from ..utils.pagination import clamp_limit
from .client import (
    BitbucketClient,
    BitbucketResourceNotFoundError,
    BitbucketResponseTooLargeError,
)

logger = logging.getLogger("mcp-atlassian.bitbucket")

# --- list_pull_requests bounds ---
# Default number of pull requests a single list_pull_requests call returns.
DEFAULT_PRS_LIMIT = 25
# Hard ceiling on pull requests returned in one call. This caps the per-call
# window (the tool's `limit`); the caller pages further with the returned cursor.
MAX_PRS_LIMIT = 100

# The enum-valued list filters, in the case the endpoint expects.
_PR_STATES = ("OPEN", "DECLINED", "MERGED", "ALL")
_PR_DIRECTIONS = ("INCOMING", "OUTGOING")
_PR_ORDERS = ("NEWEST", "OLDEST")

# --- list_user_pull_requests filters ---
# The enum-valued filters of the dashboard endpoint, in the case it expects.
# Unlike the repository list, its ``state`` has no ``ALL``. Omit it for any state.
_DASHBOARD_ROLES = ("REVIEWER", "AUTHOR", "PARTICIPANT")
_DASHBOARD_PARTICIPANT_STATUSES = ("UNAPPROVED", "NEEDS_WORK", "APPROVED")
_DASHBOARD_STATES = ("OPEN", "DECLINED", "MERGED")
_DASHBOARD_ORDERS = (
    "NEWEST",
    "OLDEST",
    "DRAFT_STATUS",
    "PARTICIPANT_STATUS",
    "CLOSED_DATE",
)

# --- get_activities bounds ---
# Default number of activity entries a single get_activities call returns.
DEFAULT_ACTIVITIES_LIMIT = 25
# Hard ceiling on activity entries returned in one call.
MAX_ACTIVITIES_LIMIT = 200

# --- comment write bounds ---
# Maximum comment text length accepted before a request is issued: the length
# at which Bitbucket Data Center rejects the request (not declared in the REST
# spec), checked client-side so an oversized body is rejected before it is sent.
MAX_COMMENT_TEXT_CHARS = 32768

# --- get_pull_request_changes bounds ---
# Default number of changed files a single get_pull_request_changes call
# returns.
DEFAULT_CHANGES_LIMIT = 25
# Hard ceiling on changed files returned in one call.
MAX_CHANGES_LIMIT = 100

# --- get_pull_request_diff bounds ---
# Default per-file diff-line cap (token control). The diff is one payload, so the
# bounds are lines per file and total files.
DEFAULT_MAX_LINES_PER_FILE = 500
# Hard ceiling on the per-file diff-line cap the caller may request.
MAX_MAX_LINES_PER_FILE = 5000
# Default and ceiling on the number of file diffs retained in one call.
DEFAULT_MAX_FILES = 100
MAX_MAX_FILES = 1000
# Ceiling on the server-side context lines requested around each change. The
# parameter exists to shrink a diff; a large value makes the server render
# most of every changed file, so the ceiling stays modest.
MAX_CONTEXT_LINES = 1000


@dataclass
class BitbucketPullRequestsPage:
    """A bounded, possibly-truncated view of a list of pull requests.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage` for the full
    meaning of the ``is_last_page`` / ``truncated`` combinations).

    Attributes:
        pull_requests: The collected pull-request models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``pull_requests`` omits PRs that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
    """

    pull_requests: list[BitbucketPullRequest]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


@dataclass
class BitbucketActivitiesPage:
    """A bounded, possibly-truncated view of a pull request's activity timeline.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage`). For the
    filtered-window case under an ``action`` filter see
    :meth:`PullRequestsMixin.get_activities`.

    Attributes:
        activities: The collected activity models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``activities`` omits entries that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
    """

    activities: list[BitbucketActivity]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


@dataclass
class BitbucketChangesPage:
    """A bounded, possibly-truncated view of a pull request's changed files.

    ``truncated`` is the authoritative completeness signal (see
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage` for the full
    meaning of the ``is_last_page`` / ``truncated`` combinations).

    Attributes:
        changes: The collected changed-file models (at most ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``changes`` omits files that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see
            :class:`~mcp_atlassian.bitbucket.client.BitbucketPage`).
    """

    changes: list[BitbucketChange]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


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
            segments validated and percent-encoded (no dot segments, no
            unescaped slashes) to prevent path traversal or query-string
            injection.

        Raises:
            ValueError: If either segment is blank, ``.``, or ``..``.
        """
        base = BitbucketClient._repo_base_path(project_key, repository_slug)
        return f"{base}/pull-requests"

    def list_pull_requests(
        self,
        project_key: str,
        repository_slug: str,
        *,
        state: str | None = None,
        direction: str | None = None,
        at: str | None = None,
        order: str | None = None,
        filter_text: str | None = None,
        draft: bool | None = None,
        start: int = 0,
        limit: int = DEFAULT_PRS_LIMIT,
    ) -> BitbucketPullRequestsPage:
        """List pull requests in a Bitbucket Data Center repository.

        Calls ``GET /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        pull-requests`` (paged with ``start``/``limit``); one window per call,
        resumable via the returned ``next_page_start``.

        The enum-valued filters (``state``, ``direction``, ``order``) are
        normalized through :meth:`BitbucketClient._enum_param`: a blank value
        is dropped and an unrecognised value raises before any request. A
        blank ``at`` or ``filter_text`` is dropped as well.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            state: Optional state filter (``OPEN``, the upstream default,
                ``DECLINED``, ``MERGED``, or ``ALL``).
            direction: Optional direction relative to the repository
                (``INCOMING``, the upstream default, or ``OUTGOING``).
            at: Optional fully-qualified branch ref to filter on (e.g.
                ``refs/heads/main``).
            order: Optional ordering (``NEWEST``, the upstream default, or
                ``OLDEST``).
            filter_text: Optional substring matched against a pull request's
                title or description, applied server-side.
            draft: Optional filter by draft status. Sent as the lowercase string
                the endpoint expects (``"true"``/``"false"``).
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of pull requests to return. Clamped to
                ``[1, MAX_PRS_LIMIT]``.

        Returns:
            A :class:`BitbucketPullRequestsPage` carrying the collected pull
            requests, whether the upstream list was fully consumed
            (``is_last_page``), whether PRs were omitted (``truncated``), and the
            ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, ``state``, ``direction``, or
                ``order`` is not a recognised value, a page is misshaped, or
                the request fails (see :meth:`BitbucketClient._get`).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        path = self._pr_base_path(project_key, repository_slug)
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.list_pull_requests"),
                MAX_PRS_LIMIT,
            ),
        )
        params: dict[str, Any] = {}
        enum_filters = (
            ("state", state, _PR_STATES),
            ("direction", direction, _PR_DIRECTIONS),
            ("order", order, _PR_ORDERS),
        )
        for name, value, allowed in enum_filters:
            normalized = self._enum_param(value, name=name, allowed=allowed)
            if normalized is not None:
                params[name] = normalized
        if at and at.strip():
            params["at"] = at.strip()
        if filter_text and filter_text.strip():
            params["filterText"] = filter_text.strip()
        if draft is not None:
            # The endpoint types draft as a string query param, so the boolean
            # is sent as its lowercase string form.
            params["draft"] = "true" if draft else "false"
        page = self._fetch_page(
            path,
            limit=limit,
            start=start,
            params=params or None,
        )
        pull_requests = [
            BitbucketPullRequest.from_api_response(value) for value in page.values
        ]
        return BitbucketPullRequestsPage(
            pull_requests=pull_requests,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    def list_user_pull_requests(
        self,
        *,
        user: str | None = None,
        role: str | None = None,
        participant_status: list[str] | None = None,
        state: str | None = None,
        order: str | None = None,
        closed_since: int | None = None,
        start: int = 0,
        limit: int = DEFAULT_PRS_LIMIT,
    ) -> BitbucketPullRequestsPage:
        """List the pull requests a user is involved in, across repositories.

        Calls ``GET /rest/api/1.0/dashboard/pull-requests`` (paged with
        ``start``/``limit``); one window per call, resumable via the returned
        ``next_page_start``. The server applies every filter and returns only
        the pull requests the authenticated caller may see, so naming another
        ``user`` does not widen access.

        The enum-valued filters (``role``, ``participant_status``, ``state``,
        ``order``) are normalized through :meth:`BitbucketClient._enum_param`,
        which drops a blank value and raises on an unrecognised one before any
        request. A blank ``user`` is dropped as well, so the server default (the
        authenticated user) applies.

        Args:
            user: Optional Bitbucket username whose pull requests to list.
                Omitted (or blank) means the authenticated user.
            role: Optional role the user holds on each pull request
                (``REVIEWER``, ``AUTHOR``, or ``PARTICIPANT``). Omit for any
                role.
            participant_status: Optional participant statuses to match, any of
                ``UNAPPROVED``, ``NEEDS_WORK``, or ``APPROVED``, sent
                comma-separated. Omit (or pass only blanks) for any status.
            state: Optional state filter (``OPEN``, ``DECLINED``, or
                ``MERGED``). Omit for any state. This endpoint has no ``ALL``.
            order: Optional ordering: ``NEWEST`` (the upstream default),
                ``OLDEST``, ``DRAFT_STATUS``, ``PARTICIPANT_STATUS``, or
                ``CLOSED_DATE``.
            closed_since: Optional window in seconds. Only pull requests closed
                within the last ``closed_since`` seconds are returned. Must be a
                positive integer.
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of pull requests to return. Clamped to
                ``[1, MAX_PRS_LIMIT]``.

        Returns:
            A :class:`BitbucketPullRequestsPage` carrying the collected pull
            requests, whether the upstream list was fully consumed
            (``is_last_page``), whether PRs were omitted (``truncated``), and the
            ``next_page_start`` resume cursor.

        Raises:
            ValueError: If ``role``, an entry of ``participant_status``,
                ``state``, or ``order`` is not a recognised value,
                ``closed_since`` is not a positive integer, a page is misshaped,
                or the request fails (see :meth:`BitbucketClient._get`).
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.list_user_pull_requests"),
                MAX_PRS_LIMIT,
            ),
        )
        # bool is an int subclass, so ``True`` would otherwise be sent as a
        # one-second window. Reject it with the other non-positive values.
        if closed_since is not None and (
            isinstance(closed_since, bool) or closed_since < 1
        ):
            raise ValueError("closed_since must be a positive integer (seconds).")
        params: dict[str, Any] = {}
        if user and user.strip():
            params["user"] = user.strip()
        enum_filters = (
            ("role", role, _DASHBOARD_ROLES),
            ("state", state, _DASHBOARD_STATES),
            ("order", order, _DASHBOARD_ORDERS),
        )
        for name, value, allowed in enum_filters:
            normalized = self._enum_param(value, name=name, allowed=allowed)
            if normalized is not None:
                params[name] = normalized
        statuses: list[str] = []
        for entry in participant_status or []:
            normalized = self._enum_param(
                entry,
                name="participant_status",
                allowed=_DASHBOARD_PARTICIPANT_STATUSES,
            )
            if normalized is not None and normalized not in statuses:
                statuses.append(normalized)
        if statuses:
            params["participantStatus"] = ",".join(statuses)
        if closed_since is not None:
            params["closedSince"] = closed_since
        page = self._fetch_page(
            "/dashboard/pull-requests",
            limit=limit,
            start=start,
            params=params or None,
        )
        pull_requests = [
            BitbucketPullRequest.from_api_response(value) for value in page.values
        ]
        return BitbucketPullRequestsPage(
            pull_requests=pull_requests,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
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
                the response is not a non-empty object, or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        data = self._get(f"{base}/{pr_id}")
        if not isinstance(data, dict) or not data:
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{base}/{pr_id}; expected a non-empty pull-request object."
            )
        return BitbucketPullRequest.from_api_response(data)

    def get_pull_request_merge_status(
        self, project_key: str, repository_slug: str, pull_request_id: int | str
    ) -> BitbucketMergeStatus:
        """Get whether a pull request can merge: conflicts and merge-check vetoes.

        Calls ``GET .../pull-requests/{pullRequestId}/merge`` (one request).
        The endpoint answers only for an open pull request. For a merged or
        declined one the server replies 409 and the raised error carries the
        instance's own message.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketMergeStatus`.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                the response is not a non-empty object, the pull request is not
                open (HTTP 409), or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        path = f"{base}/{pr_id}/merge"
        data = self._get(path)
        if not isinstance(data, dict) or not data:
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a non-empty mergeability object."
            )
        return BitbucketMergeStatus.from_api_response(data)

    def get_pull_request_diff(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        *,
        max_lines_per_file: int = DEFAULT_MAX_LINES_PER_FILE,
        max_files: int = DEFAULT_MAX_FILES,
        context_lines: int | None = None,
        path: str | None = None,
        src_path: str | None = None,
    ) -> BitbucketPullRequestDiff:
        """Get a pull request's structured diff, bounded for token control.

        Without ``path`` this calls the whole-PR form ``GET
        .../pull-requests/{pullRequestId}/diff``, whose body is a
        ``RestDiffResponse`` holding one ``RestDiff`` per file. With ``path``
        it calls ``GET .../diff/{path}``, whose body is the single ``RestDiff``
        for that file; the result is wrapped as a one-file diff so both forms
        share a shape. Either form is one request. ``withComments`` is forced
        off to keep inline comments out of the payload. The endpoint has no
        server-side size parameter, so the download is bounded by the client's
        byte cap; a larger body raises with the narrowing options that are
        available: list the changed files, fetch one file by ``path``, or
        lower ``context_lines``. The per-file line budget and file cap are
        applied at model-build time and surfaced via the ``truncated`` flags.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            max_lines_per_file: Per-file diff-line cap. Clamped to
                ``[1, MAX_MAX_LINES_PER_FILE]``.
            max_files: Maximum number of file diffs to retain. Clamped to
                ``[1, MAX_MAX_FILES]``. Ignored when ``path`` is set.
            context_lines: Optional number of context lines around each change,
                sent server-side as ``contextLines``. Clamped to
                ``[0, MAX_CONTEXT_LINES]``; None uses the server default.
            path: Optional file path to diff on its own. Component rules are
                those of :meth:`BitbucketClient._encode_repo_path`.
            src_path: The file's previous path when it was moved, copied, or
                renamed; sent as ``srcPath``. Requires ``path``.

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequestDiff`.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                a path component is invalid, ``src_path`` is given without
                ``path``, the response is not a JSON object, or the request
                fails.
            BitbucketResponseTooLargeError: If the body exceeds the byte cap
                (a ValueError subclass); the message names the narrowing
                options that apply to the form that was called.
            BitbucketResourceNotFoundError: If the pull request or file does
                not exist or is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        max_lines_per_file = max(1, min(max_lines_per_file, MAX_MAX_LINES_PER_FILE))
        max_files = max(1, min(max_files, MAX_MAX_FILES))
        params: dict[str, Any] = {"withComments": "false"}
        if context_lines is not None:
            params["contextLines"] = max(0, min(context_lines, MAX_CONTEXT_LINES))
        encoded_path = self._encode_repo_path(path, what="path")
        # src_path travels as a query value, which the HTTP layer encodes; it
        # is validated with the same component rules but sent unencoded.
        src_path_text = src_path.strip() if src_path else ""
        if src_path_text:
            self._encode_repo_path(src_path_text, what="src_path")
            if not encoded_path:
                raise ValueError("src_path requires path (the file's current path).")
            params["srcPath"] = src_path_text
        url = f"{base}/{pr_id}/diff" + (f"/{encoded_path}" if encoded_path else "")
        try:
            data = self._get(url, params=params)
        except BitbucketResourceNotFoundError as e:
            if not encoded_path:
                raise
            raise BitbucketResourceNotFoundError(
                f"Bitbucket resource not found (HTTP 404) for {url}. The pull "
                "request does not exist, the authenticated user lacks "
                "permission to view it, or the pull request does not change "
                f"the file '{path}' (check the changed-file listing, and set "
                "src_path for a moved file)."
            ) from e
        except BitbucketResponseTooLargeError as e:
            if encoded_path:
                options = "Available option: lower context_lines."
            else:
                options = (
                    "Available options: list the changed files with "
                    "bitbucket_get_pull_request_changes, fetch one file with "
                    "bitbucket_get_pull_request_diff and its path, or lower "
                    "context_lines."
                )
            raise BitbucketResponseTooLargeError(f"{e} {options}") from e
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{url}; expected a diff object."
            )
        if not encoded_path:
            return BitbucketPullRequestDiff.from_api_response(
                data, max_lines_per_file=max_lines_per_file, max_files=max_files
            )
        # The single-file form answers with a bare RestDiff. A body carrying
        # the whole-PR ``diffs`` envelope is accepted as well, since the
        # endpoint has no live capture; any other shape is a parse failure
        # and must not read as an empty diff.
        if isinstance(data.get("diffs"), list):
            return BitbucketPullRequestDiff.from_api_response(
                data, max_lines_per_file=max_lines_per_file, max_files=1
            )
        if not any(key in data for key in ("hunks", "source", "destination")):
            raise ValueError(
                "Bitbucket returned an unexpected diff response shape for "
                f"{url}: neither a diff object nor a 'diffs' list. Refusing to "
                "report the malformed body as an empty diff."
            )
        file_diff = BitbucketFileDiff.from_api_response(
            data, max_lines_per_file=max_lines_per_file
        )
        return BitbucketPullRequestDiff(
            files=[file_diff],
            total_files=1,
            truncated=file_diff.line_truncated or file_diff.server_truncated,
        )

    def get_pull_request_changes(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        *,
        start: int = 0,
        limit: int = DEFAULT_CHANGES_LIMIT,
    ) -> BitbucketChangesPage:
        """List the files a Bitbucket Data Center pull request changes.

        Calls ``GET .../pull-requests/{pullRequestId}/changes`` (paged with
        ``start``/``limit``); one window per call, resumable via the returned
        ``next_page_start``. ``withComments`` is forced off so the server
        skips per-file comment counting. The listing is the discovery step
        for a per-file diff: pass an entry's ``path`` (and ``src_path`` for a
        move or copy) to :meth:`get_pull_request_diff`.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            pull_request_id: The pull-request id (positive integer).
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of changed files to return. Clamped to
                ``[1, MAX_CHANGES_LIMIT]``.

        Returns:
            A :class:`BitbucketChangesPage` carrying the collected changes,
            whether the upstream list was fully consumed (``is_last_page``),
            whether files were omitted (``truncated``), and the
            ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                a page is misshaped, or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.get_pull_request_changes"),
                MAX_CHANGES_LIMIT,
            ),
        )
        page = self._fetch_page(
            f"{base}/{pr_id}/changes",
            limit=limit,
            start=start,
            params={"withComments": "false"},
        )
        changes = [BitbucketChange.from_api_response(value) for value in page.values]
        return BitbucketChangesPage(
            changes=changes,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    def get_activities(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        *,
        action: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_ACTIVITIES_LIMIT,
    ) -> BitbucketActivitiesPage:
        """List a pull request's activity timeline (comments + approvals + …).

        Calls ``GET .../pull-requests/{pullRequestId}/activities`` (paged);
        one window per call, resumable via the returned ``next_page_start``.
        The endpoint exposes no action query filter, so an ``action`` (the
        mechanism behind the comments view, ``action="COMMENTED"``) narrows the
        fetched window client-side: the returned entries are post-filter, so a
        window can be empty while more pages remain. Keep paging with
        ``start=next_page_start`` until ``is_last_page``.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            action: Optional activity action to filter to (e.g. ``"COMMENTED"``),
                matched case-insensitively.
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of activity entries to return. Clamped to
                ``[1, MAX_ACTIVITIES_LIMIT]``.

        Returns:
            A :class:`BitbucketActivitiesPage` carrying the collected activities,
            whether the upstream list was fully consumed (``is_last_page``),
            whether entries were omitted (``truncated``), and the
            ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                a page is misshaped, or the request fails.
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.get_activities"),
                MAX_ACTIVITIES_LIMIT,
            ),
        )

        # An ``action`` narrows the fetched window client-side; the raw
        # upstream cursor is returned even when the narrowed window is empty.
        transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
        if action is not None:
            wanted = action.strip().upper()

            def _filter(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [v for v in values if str(v.get("action", "")).upper() == wanted]

            transform = _filter

        page = self._fetch_page(
            f"{base}/{pr_id}/activities",
            limit=limit,
            start=start,
            transform=transform,
        )
        activities = [
            BitbucketActivity.from_api_response(value) for value in page.values
        ]
        return BitbucketActivitiesPage(
            activities=activities,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    @staticmethod
    def _check_comment_text(text: str) -> None:
        """Reject blank or oversized comment text before any request.

        Args:
            text: The comment text to validate.

        Raises:
            ValueError: If ``text`` is blank or longer than
                ``MAX_COMMENT_TEXT_CHARS``.
        """
        if not text or not text.strip():
            raise ValueError("text must be a non-blank comment string.")
        if len(text) > MAX_COMMENT_TEXT_CHARS:
            raise ValueError(
                f"text is {len(text)} characters; Bitbucket accepts at most "
                f"{MAX_COMMENT_TEXT_CHARS}. Shorten the comment or split it."
            )

    @staticmethod
    def _build_comment_body(
        text: str,
        *,
        parent_id: int | None,
        file_path: str | None,
        line: int | None,
        line_type: str | None,
        file_type: str | None,
        severity: str,
    ) -> dict[str, Any]:
        """Validate the comment-mode inputs and build the POST request body.

        The comment mode is selected by which optional params are present,
        mirroring the operation description's request examples: a general
        comment, a reply (``parent``), a whole-file or line ``anchor``, and a
        BLOCKER task. Invalid combinations are rejected client-side with a clear
        ValueError; the server stays authoritative for everything else (e.g.
        whether the anchor actually lands on the diff).

        No ``diffType``, ``fromHash``, or ``toHash`` is emitted, so the server
        resolves an anchor against the PR's EFFECTIVE diff, the same frame
        ``get_pull_request_diff`` reads, and no commit-hash bookkeeping is
        needed.

        Args:
            text: The comment text; must be non-blank.
            parent_id: When set, a reply to that comment (no anchor allowed).
            file_path: The file to anchor on (whole-file, or with ``line``).
            line: The 1-based diff line to anchor on (requires ``file_path``).
            line_type: ``ADDED``/``REMOVED``/``CONTEXT`` (required with ``line``).
            file_type: ``FROM``/``TO``; defaults from ``line_type`` when omitted.
            severity: ``NORMAL`` or ``BLOCKER`` (a must-resolve task).

        Returns:
            The JSON request body for the comment POST.

        Raises:
            ValueError: If ``text`` is blank, an enum is invalid, or the
                mode params are combined illegally (a reply cannot carry
                anchor params; ``line`` requires ``file_path``;
                ``line_type``/``file_type`` require ``line``).
        """
        PullRequestsMixin._check_comment_text(text)

        normalized_severity = severity.strip().upper()
        if normalized_severity not in ("NORMAL", "BLOCKER"):
            raise ValueError("severity must be 'NORMAL' or 'BLOCKER'.")

        anchor_params_present = any(
            param is not None for param in (file_path, line, line_type, file_type)
        )

        # A reply inherits its parent thread's anchor, so it carries none itself.
        if parent_id is not None:
            if anchor_params_present:
                raise ValueError(
                    "parent_id (a reply) cannot be combined with anchor params "
                    "(file_path/line/line_type/file_type)."
                )
            if parent_id <= 0:
                raise ValueError("parent_id must be a positive integer.")

        if file_path is not None and not file_path.strip():
            raise ValueError("file_path must be a non-blank file path.")
        if line is not None and file_path is None:
            raise ValueError(
                "line requires file_path (a line comment anchors to a file)."
            )
        if (line_type is not None or file_type is not None) and line is None:
            raise ValueError("line_type/file_type are only valid together with line.")

        body: dict[str, Any] = {"text": text}
        if normalized_severity == "BLOCKER":
            body["severity"] = "BLOCKER"

        if parent_id is not None:
            body["parent"] = {"id": parent_id}
            return body

        if file_path is None:
            return body  # general comment (optionally a task)

        anchor: dict[str, Any] = {"path": file_path}
        if line is not None:
            if line < 1:
                raise ValueError("line must be a positive integer (1-based).")
            if line_type is None:
                raise ValueError("line_type is required for a line comment.")
            normalized_line_type = line_type.strip().upper()
            if normalized_line_type not in ("ADDED", "REMOVED", "CONTEXT"):
                raise ValueError("line_type must be 'ADDED', 'REMOVED', or 'CONTEXT'.")
            if file_type is not None:
                normalized_file_type = file_type.strip().upper()
                if normalized_file_type not in ("FROM", "TO"):
                    raise ValueError("file_type must be 'FROM' or 'TO'.")
            else:
                # Default to the side the structured diff reader presents the
                # line on: source side for a removed line, destination side for
                # an added or context line.
                normalized_file_type = (
                    "FROM" if normalized_line_type == "REMOVED" else "TO"
                )
            anchor["line"] = line
            anchor["lineType"] = normalized_line_type
            anchor["fileType"] = normalized_file_type
        body["anchor"] = anchor
        return body

    def add_comment(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        text: str,
        *,
        parent_id: int | None = None,
        file_path: str | None = None,
        line: int | None = None,
        line_type: str | None = None,
        file_type: str | None = None,
        severity: str = "NORMAL",
    ) -> BitbucketComment:
        """Add a comment to a Bitbucket Data Center pull request.

        Calls ``POST .../pull-requests/{pullRequestId}/comments``. The comment
        mode follows which optional params are supplied (general / reply /
        whole-file / line / BLOCKER task); see :meth:`_build_comment_body` for
        the validation matrix and the EFFECTIVE-anchor behaviour. Despite being
        a write, the endpoint only requires ``REPO_READ`` on Data Center.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            text: The comment text; must be non-blank.
            parent_id: When set, reply to that comment id.
            file_path: When set, anchor the comment to this file.
            line: When set (with ``file_path``), anchor to this 1-based diff line.
            line_type: ``ADDED``/``REMOVED``/``CONTEXT`` (required with ``line``).
            file_type: ``FROM``/``TO``; derived from ``line_type`` when omitted.
            severity: ``NORMAL`` (default) or ``BLOCKER`` (a must-resolve task).

        Returns:
            The created :class:`~mcp_atlassian.models.bitbucket.BitbucketComment`
            (carrying its ``id`` and ``version`` for later edit/delete).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                the mode params are combined illegally, the 2xx body lacks a
                positive integer ``id`` or an integer ``version`` (the write
                may have been applied but was not confirmed), or the request
                fails (a 400/409 surfaces the instance's own message).
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        body = self._build_comment_body(
            text,
            parent_id=parent_id,
            file_path=file_path,
            line=line,
            line_type=line_type,
            file_type=file_type,
            severity=severity,
        )
        data = self._post(f"{base}/{pr_id}/comments", json_body=body)
        return self._confirmed_comment(data, f"{base}/{pr_id}/comments")

    @staticmethod
    def _shown(value: Any) -> str:
        """Return ``repr(value)`` capped at 64 characters for an error message."""
        shown = repr(value)
        return shown[:64] + "..." if len(shown) > 64 else shown

    @staticmethod
    def _confirmed_comment(
        data: Any,
        path: str,
        *,
        comment_id: int | None = None,
        sent_version: int | None = None,
        text: str | None = None,
        thread_resolved: bool | None = None,
    ) -> BitbucketComment:
        """Parse a 2xx comment write body, requiring its acknowledgement fields.

        A comment create or update is confirmed only by a ``RestComment`` body
        carrying a positive integer ``id`` and an integer ``version`` (``0`` is
        the version of a freshly created comment). An update is confirmed
        against the request as well: the ``id`` equals the updated comment's,
        the ``version`` is at least the one sent (equal for a no-op such as
        resolving an already-resolved thread), and each field that was sent is
        echoed back with the sent value. Any other 2xx body is reported as an
        error rather than a comment with a made-up id or an unverified state.

        Args:
            data: The parsed JSON body of the write.
            path: The API path, for the error message.
            comment_id: For an update, the id of the comment that was updated.
            sent_version: For an update, the optimistic-lock version sent.
            text: The text sent, when the request carried one.
            thread_resolved: The thread state sent, when the request carried
                one.

        Returns:
            The confirmed :class:`~mcp_atlassian.models.bitbucket.BitbucketComment`.

        Raises:
            ValueError: If the body is not an object, lacks a positive integer
                ``id`` or an integer ``version``, or disagrees with the
                request on the id, the version, the text, or the thread
                state. The write may have been applied on the server; the
                message says so.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a comment object."
            )
        returned_id = data.get("id")
        version = data.get("version")
        id_ok = (
            isinstance(returned_id, int)
            and not isinstance(returned_id, bool)
            and returned_id > 0
        )
        version_ok = isinstance(version, int) and not isinstance(version, bool)
        if not (id_ok and version_ok):
            raise ValueError(
                f"Bitbucket returned an incomplete comment body for {path}; "
                "expected a positive integer 'id' and an integer 'version'. "
                "The write may have been applied but was not confirmed."
            )
        mismatch: str | None = None
        if comment_id is not None and returned_id != comment_id:
            mismatch = f"'id' {comment_id} but got {returned_id}"
        elif sent_version is not None and version < sent_version:
            mismatch = f"a 'version' of at least {sent_version} but got {version}"
        elif text is not None and data.get("text") != text:
            shown = PullRequestsMixin._shown(data.get("text"))
            mismatch = f"'text' to echo the sent text but got {shown}"
        elif thread_resolved is not None and (
            data.get("threadResolved") is not thread_resolved
        ):
            shown = PullRequestsMixin._shown(data.get("threadResolved"))
            mismatch = f"'threadResolved' {thread_resolved} but got {shown}"
        if mismatch is not None:
            raise ValueError(
                f"Bitbucket returned an unconfirmed comment body for {path}; "
                f"expected {mismatch}. The write may have been applied but was "
                "not confirmed."
            )
        return BitbucketComment.from_api_response(data)

    def update_comment(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        comment_id: int | str,
        *,
        version: int,
        text: str | None = None,
        thread_resolved: bool | None = None,
    ) -> BitbucketComment:
        """Edit a pull-request comment's text and/or resolve its thread.

        Calls ``PUT .../pull-requests/{pullRequestId}/comments/{commentId}`` with
        the optimistic-lock ``version`` and the changed field(s) in the JSON body.
        Bitbucket DC has no separate resolve endpoint: resolving a thread is
        ``PUT {version, threadResolved: true}`` (``false`` reopens it), editing
        text is ``PUT {version, text}``; both may be sent in one call. A
        successful ``PUT`` returns the updated ``RestComment``; its
        ``version`` is bumped by a real change and unchanged by a no-op. Only the comment author may edit its *text* (a 401).
        Anyone with access may toggle the thread state.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            comment_id: The comment id to update.
            version: The comment's current ``version`` (from ``add_comment`` or
                the ``COMMENTED`` entries of ``get_activities``); a stale value
                yields a 409.
            text: When set, the new comment text.
            thread_resolved: When set, resolve (``True``) or reopen (``False``)
                the comment's thread.

        Returns:
            The updated :class:`~mcp_atlassian.models.bitbucket.BitbucketComment`
            (carrying the resulting ``version`` and ``thread_resolved``).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                ``version`` is not an int, neither ``text`` nor
                ``thread_resolved`` is provided, the 2xx body lacks a positive
                integer ``id`` or an integer ``version`` or disagrees with the
                request on the id, the version, the text, or the thread state
                (the write may have been applied but was not
                confirmed), or the request fails (a 409 stale-version
                surfaces the instance's own message).
            BitbucketResourceNotFoundError: If the pull request or comment does
                not exist or is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        cid = self._coerce_comment_id(comment_id)
        comment_path = self._comment_path(base, pr_id, cid)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("version must be an integer (the comment's version).")
        if text is None and thread_resolved is None:
            raise ValueError(
                "at least one of text or thread_resolved must be provided."
            )
        if text is not None:
            self._check_comment_text(text)

        body: dict[str, Any] = {"version": version}
        if text is not None:
            body["text"] = text
        if thread_resolved is not None:
            body["threadResolved"] = thread_resolved
        data = self._put(comment_path, json_body=body)
        return self._confirmed_comment(
            data,
            comment_path,
            comment_id=cid,
            sent_version=version,
            text=text,
            thread_resolved=thread_resolved,
        )

    def delete_comment(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        comment_id: int | str,
        *,
        version: int,
    ) -> None:
        """Delete a pull-request comment.

        Calls ``DELETE .../pull-requests/{pullRequestId}/comments/{commentId}``
        with the optimistic-lock ``version`` as a query parameter; a successful
        delete returns ``204`` with no body. Delete does not cascade. A 409
        may mean the ``version`` is stale, the comment has replies, or the
        repository is archived. The instance's own message says which.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            comment_id: The comment id to delete.
            version: The comment's current ``version`` (from ``add_comment`` or
                the ``COMMENTED`` entries of ``get_activities``).

        Returns:
            None, since a successful delete has no body.

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                ``version`` is not an int, or the request fails (a 409 surfaces
                the instance's own message).
            BitbucketResourceNotFoundError: If the pull request or comment does
                not exist or is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        comment_path = self._comment_path(base, pr_id, comment_id)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("version must be an integer (the comment's version).")
        self._delete(comment_path, params={"version": version})

    @staticmethod
    def _coerce_comment_id(comment_id: int | str) -> int:
        """Coerce a caller-supplied comment id to a positive integer.

        Args:
            comment_id: The comment id; a numeric string like ``"5"`` is
                accepted.

        Returns:
            The positive integer id.

        Raises:
            ValueError: If ``comment_id`` is not a positive integer.
        """
        try:
            cid = int(comment_id)
        except (TypeError, ValueError):
            raise ValueError("comment_id must be a positive integer.") from None
        if cid <= 0:
            raise ValueError("comment_id must be a positive integer.")
        return cid

    @staticmethod
    def _comment_path(base: str, pr_id: int, comment_id: int | str) -> str:
        """Build the comment-scoped REST path for a positive-integer comment id.

        Args:
            base: The PR collection path from :meth:`_pr_base_path`.
            pr_id: The validated positive-integer pull-request id.
            comment_id: The caller-supplied comment id (see
                :meth:`_coerce_comment_id`).

        Returns:
            ``{base}/{pr_id}/comments/{commentId}``.

        Raises:
            ValueError: If ``comment_id`` is not a positive integer.
        """
        cid = PullRequestsMixin._coerce_comment_id(comment_id)
        return f"{base}/{pr_id}/comments/{cid}"

    def set_review_status(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        status: str,
    ) -> dict[str, Any]:
        """Set the authenticated user's review status on a pull request.

        Calls ``PUT .../pull-requests/{id}/participants/{userSlug}`` with
        ``{"status": ...}``, where the slug is the authenticated caller's
        (resolved via :meth:`_resolve_current_user_slug`). ``NEEDS_WORK`` is the
        API name for the UI's "Request changes"; ``UNAPPROVED`` withdraws a prior
        approval. Bitbucket forbids the pull request **author** from setting a
        status. That rejection surfaces with the server's own message. On Data
        Center this needs only ``REPO_READ``.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            status: ``APPROVED``, ``NEEDS_WORK``, or ``UNAPPROVED``.

        Returns:
            A simplified dict with the confirmed ``status`` (plus ``approved``
            and the participant ``user`` when present).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                the status is not a valid enum, the caller's identity/slug cannot
                be resolved, the 2xx body's ``status`` differs from the requested
                one (the write may have been applied but was not confirmed), or
                the request fails (a 400/409, for example the author setting a
                status, surfaces the instance's own message).
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        normalized = status.strip().upper()
        if normalized not in ("APPROVED", "NEEDS_WORK", "UNAPPROVED"):
            raise ValueError(
                "status must be 'APPROVED', 'NEEDS_WORK', or 'UNAPPROVED'."
            )

        user_slug = self._resolve_current_user_slug()
        encoded_slug = self._encode_segment(
            user_slug, name="user slug", what="user slug"
        )
        path = f"{base}/{pr_id}/participants/{encoded_slug}"
        data = self._put(path, json_body={"status": normalized})
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a participant object."
            )

        confirmed = data.get("status")
        if confirmed != normalized:
            raise ValueError(
                f"Bitbucket returned an incomplete participant body for {path}; "
                f"expected status '{normalized}' but got {self._shown(confirmed)}. "
                "The write may have been applied but was not confirmed."
            )

        # Project explicitly rather than echo the raw participant body.
        participant: dict[str, Any] = {"status": confirmed}
        if "approved" in data:
            participant["approved"] = data.get("approved")
        user_data = data.get("user")
        if isinstance(user_data, dict):
            participant["user"] = BitbucketUser.from_api_response(
                user_data
            ).to_simplified_dict()
        return participant
