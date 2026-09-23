"""Bitbucket Data Center pull-request operations."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..models.bitbucket import (
    BitbucketActivity,
    BitbucketChange,
    BitbucketComment,
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

# --- task comments ---
# The ``state`` of a BLOCKER comment (a task), in the case the endpoint expects.
_TASK_STATES = ("RESOLVED", "OPEN")

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

# --- pull-request write bounds ---
# Maximum pull-request title length accepted before a request is issued.
# Bitbucket Data Center rejects a longer title (the limit is not declared in the
# REST spec), so the check runs client-side and an oversized body is not sent.
MAX_PR_TITLE_CHARS = 255
# Maximum pull-request description length, on the same footing as the title.
MAX_PR_DESCRIPTION_CHARS = 32768
# Maximum number of reviewers accepted in one request. The server resolves each
# name with a user lookup, so the list is bounded before it is sent.
MAX_PR_REVIEWERS = 50

# Git ref namespaces. A bare name is qualified into ``_HEADS_PREFIX``. A value
# already under ``_REFS_PREFIX`` is sent as given.
_REFS_PREFIX = "refs/"
_HEADS_PREFIX = "refs/heads/"
_TAGS_PREFIX = "refs/tags/"

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
        return self._build_diff(
            data,
            url,
            single_file=bool(encoded_path),
            max_lines_per_file=max_lines_per_file,
            max_files=max_files,
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
        state: str | None = None,
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
            state: The task state sent, when the request carried one.

        Returns:
            The confirmed :class:`~mcp_atlassian.models.bitbucket.BitbucketComment`.

        Raises:
            ValueError: If the body is not an object, lacks a positive integer
                ``id`` or an integer ``version``, or disagrees with the
                request on the id, the version, the text, the thread state,
                or the task state. The write may have been applied on the
                server. The message says so.
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
        elif state is not None and data.get("state") != state:
            shown = PullRequestsMixin._shown(data.get("state"))
            mismatch = f"'state' {state!r} but got {shown}"
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

    def set_task_state(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        comment_id: int | str,
        *,
        version: int,
        state: str,
    ) -> BitbucketComment:
        """Resolve or reopen a pull-request task (a ``BLOCKER`` comment).

        Calls ``PUT .../pull-requests/{pullRequestId}/comments/{commentId}`` with
        ``{version, state}``. A task is a comment whose ``severity`` is
        ``BLOCKER``, and its ``state`` is ``OPEN`` or ``RESOLVED``. This is the
        task state, distinct from the thread's ``threadResolved`` flag that
        :meth:`update_comment` toggles. The request carries only ``version`` and
        ``state``, so ``text`` and ``severity`` are left unchanged. The endpoint
        needs ``REPO_READ``, and Bitbucket lets the comment author, the
        pull-request author, or a repository admin change ``state``. What the
        server does with ``state`` on a ``NORMAL`` comment is not documented.
        The response is confirmed against the request either way, so an
        unchanged ``state`` is reported as an unconfirmed write.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            comment_id: The task comment id.
            version: The comment's current ``version`` (from ``add_comment`` or
                ``get_activities``). A stale value yields a 409.
            state: ``RESOLVED`` or ``OPEN`` (case-insensitive).

        Returns:
            The updated :class:`~mcp_atlassian.models.bitbucket.BitbucketComment`
            (carrying the resulting ``version`` and ``state``).

        Raises:
            ValueError: If a segment is blank, an id is not a positive integer,
                ``version`` is not an int, ``state`` is not ``RESOLVED`` or
                ``OPEN``, the 2xx body lacks a positive integer ``id`` or an
                integer ``version`` or disagrees with the request on the id,
                the version, or the state (the write may have been applied but
                was not confirmed), or the request fails (a 409 for a stale
                version carries the instance's own message).
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
        if not isinstance(state, str):
            raise ValueError("state must be 'RESOLVED' or 'OPEN'.")
        normalized_state = state.strip().upper()
        if normalized_state not in _TASK_STATES:
            raise ValueError("state must be 'RESOLVED' or 'OPEN'.")

        body: dict[str, Any] = {"version": version, "state": normalized_state}
        data = self._put(comment_path, json_body=body)
        return self._confirmed_comment(
            data,
            comment_path,
            comment_id=cid,
            sent_version=version,
            state=normalized_state,
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

    @staticmethod
    def _normalise_ref(value: str | None, *, name: str, allow_tag: bool) -> str:
        """Qualify a caller-supplied ref for a pull-request body.

        A bare name (``main``) becomes ``refs/heads/main``. A value that
        already starts with ``refs/`` is sent as given, so ``refs/tags/v1`` and
        an unusual namespace both pass through. Only a branch can be a
        pull-request target, so with ``allow_tag`` false a ``refs/tags/``
        value is rejected before any request. Whether the ref exists is left
        to the server (a 404).

        Args:
            value: The caller-supplied ref name.
            name: The parameter name used in the error message.
            allow_tag: Whether a ``refs/tags/`` value is acceptable.

        Returns:
            The fully-qualified ref id.

        Raises:
            ValueError: If the value is blank, or is a tag where only a branch
                is allowed.
        """
        text = value.strip() if value else ""
        if not text:
            raise ValueError(f"{name} must be a non-empty branch or tag name.")
        if not text.startswith(_REFS_PREFIX):
            text = f"{_HEADS_PREFIX}{text}"
        if not allow_tag and text.startswith(_TAGS_PREFIX):
            raise ValueError(
                f"{name} must be a branch. Bitbucket does not accept a tag as a "
                "pull-request target."
            )
        return text

    @staticmethod
    def _ref_object(ref_id: str, repo: tuple[str, str] | None) -> dict[str, Any]:
        """Build a ``fromRef``/``toRef`` object, naming the repository when known."""
        ref: dict[str, Any] = {"id": ref_id}
        if repo is not None:
            key, slug = repo
            ref["repository"] = {"slug": slug, "project": {"key": key}}
        return ref

    @staticmethod
    def _build_pull_request_body(
        *,
        title: str | None = None,
        description: str | None = None,
        draft: bool | None = None,
        from_ref: str | None = None,
        to_ref: str | None = None,
        reviewers: list[str] | None = None,
        target_repo: tuple[str, str] | None = None,
        from_repo: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate pull-request fields and build a ``RestPullRequest`` body.

        Only the fields that are not ``None`` are emitted, so the same builder
        serves a body that carries every field and one that carries a subset.
        A ``title`` is stripped and must be a single non-blank line within
        ``MAX_PR_TITLE_CHARS``. A ``description`` is sent as given within
        ``MAX_PR_DESCRIPTION_CHARS``. Refs go through :meth:`_normalise_ref`
        (``from_ref`` may be a tag, ``to_ref`` must be a branch) and carry a
        ``repository`` object when the repository is known: ``to_ref`` names
        ``target_repo``, ``from_ref`` names ``from_repo`` when given (a fork
        in the same hierarchy) and otherwise ``target_repo``. ``reviewers``
        are sent as ``[{"user": {"name": ...}}]`` with repeated names dropped
        and at most ``MAX_PR_REVIEWERS`` entries. An empty list is sent as-is
        (the server reads it as no reviewers), a blank entry raises, and
        resolving the names is left to the server (a 409 names the unresolved
        reviewer).

        Args:
            title: The pull-request title.
            description: The pull-request description (Markdown).
            draft: Whether the pull request is a draft.
            from_ref: The source branch or tag name.
            to_ref: The target branch name.
            reviewers: User names to add as reviewers.
            target_repo: The ``(project_key, repository_slug)`` of the target
                repository, already validated as path segments.
            from_repo: The ``(project_key, repository_slug)`` of the source
                repository when it differs from the target.

        Returns:
            The JSON request body carrying the supplied fields.

        Raises:
            ValueError: If ``title`` is blank, spans lines, or is too long,
                ``description`` is too long, a ref is blank, ``to_ref`` is a
                tag, a reviewer entry is blank, or there are too many
                reviewers.
        """
        body: dict[str, Any] = {}
        if title is not None:
            text = title.strip()
            if not text:
                raise ValueError("title must be a non-blank string.")
            if "\n" in text or "\r" in text:
                raise ValueError(
                    "title must be a single line; put further text in description."
                )
            if len(text) > MAX_PR_TITLE_CHARS:
                raise ValueError(
                    f"title is {len(text)} characters; Bitbucket accepts at most "
                    f"{MAX_PR_TITLE_CHARS}. Shorten the title."
                )
            body["title"] = text
        if description is not None:
            if len(description) > MAX_PR_DESCRIPTION_CHARS:
                raise ValueError(
                    f"description is {len(description)} characters; Bitbucket "
                    f"accepts at most {MAX_PR_DESCRIPTION_CHARS}. Shorten it."
                )
            body["description"] = description
        if draft is not None:
            body["draft"] = draft
        if from_ref is not None:
            source_id = PullRequestsMixin._normalise_ref(
                from_ref, name="from_ref", allow_tag=True
            )
            body["fromRef"] = PullRequestsMixin._ref_object(
                source_id, from_repo if from_repo is not None else target_repo
            )
        if to_ref is not None:
            target_id = PullRequestsMixin._normalise_ref(
                to_ref, name="to_ref", allow_tag=False
            )
            body["toRef"] = PullRequestsMixin._ref_object(target_id, target_repo)
        if reviewers is not None:
            seen: dict[str, None] = {}
            for entry in reviewers:
                name = entry.strip()
                if not name:
                    raise ValueError("reviewers must be non-blank user names.")
                seen.setdefault(name, None)
            if len(seen) > MAX_PR_REVIEWERS:
                raise ValueError(
                    f"reviewers lists {len(seen)} distinct users; at most "
                    f"{MAX_PR_REVIEWERS} are accepted in one request."
                )
            body["reviewers"] = [{"user": {"name": name}} for name in seen]
        return body

    @staticmethod
    def _confirmed_pull_request(
        data: Any,
        path: str,
        *,
        title: str | None = None,
        from_ref_id: str | None = None,
        to_ref_id: str | None = None,
        draft: bool | None = None,
        description: str | None = None,
        reviewers_cleared: bool = False,
        sent_version: int | None = None,
        pr_id: int | None = None,
    ) -> BitbucketPullRequest:
        """Parse a 2xx pull-request write body, requiring its acknowledgements.

        A pull-request write is confirmed only by a ``RestPullRequest`` body
        carrying a positive integer ``id`` and an integer ``version``, and by
        each field that was sent being echoed back: the ``title``, the
        fully-qualified ``fromRef.id`` and ``toRef.id``, the ``draft`` flag,
        the ``description`` (a cleared description may come back absent),
        and an empty ``reviewers`` list when the request cleared the
        reviewers. A non-empty reviewer list is not compared, because
        Bitbucket drops the author from it without an error. An update also
        expects ``id`` to be the pull request written and ``version`` to be
        the one sent plus one, since each accepted update bumps it once. A
        missing or mismatched field, including one a server ignores (a
        version without draft pull requests), raises an unconfirmed-write
        error. The write may already have been applied.
        This follows :meth:`_confirmed_comment`.

        Args:
            data: The parsed JSON body of the write.
            path: The API path, for the error message.
            title: The title sent, when the request carried one.
            from_ref_id: The qualified source ref sent, when the request
                carried one.
            to_ref_id: The qualified target ref sent, when the request carried
                one.
            draft: The draft flag sent, when the request carried one.
            description: The description sent, when the request carried one.
            reviewers_cleared: Whether the request sent an empty reviewer
                list, which the body must echo as no reviewers.
            sent_version: For an update, the optimistic-lock version sent.
            pr_id: For an update, the id of the pull request written.

        Returns:
            The confirmed
            :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`.

        Raises:
            ValueError: If the body is not an object, lacks a positive integer
                ``id`` or an integer ``version``, or disagrees with the request
                on the id, the title, a ref, the draft flag, the description,
                the cleared reviewers, or the version. The write may have been
                applied on the server. The message says so.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a pull-request object."
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
                f"Bitbucket returned an incomplete pull-request body for {path}; "
                "expected a positive integer 'id' and an integer 'version'. "
                "The write may have been applied but was not confirmed."
            )

        def _ref_id(field: str) -> Any:
            ref = data.get(field)
            return ref.get("id") if isinstance(ref, dict) else None

        mismatch: str | None = None
        if pr_id is not None and returned_id != pr_id:
            mismatch = f"'id' {pr_id} but got {returned_id}"
        elif title is not None and data.get("title") != title:
            shown = PullRequestsMixin._shown(data.get("title"))
            mismatch = f"'title' to echo the sent title but got {shown}"
        elif from_ref_id is not None and _ref_id("fromRef") != from_ref_id:
            sent = PullRequestsMixin._shown(from_ref_id)
            shown = PullRequestsMixin._shown(_ref_id("fromRef"))
            mismatch = f"'fromRef.id' {sent} but got {shown}"
        elif to_ref_id is not None and _ref_id("toRef") != to_ref_id:
            sent = PullRequestsMixin._shown(to_ref_id)
            shown = PullRequestsMixin._shown(_ref_id("toRef"))
            mismatch = f"'toRef.id' {sent} but got {shown}"
        elif draft is not None and data.get("draft") is not draft:
            shown = PullRequestsMixin._shown(data.get("draft"))
            mismatch = f"'draft' {draft} but got {shown}"
        elif (
            description is not None
            and (data.get("description") if data.get("description") is not None else "")
            != description
        ):
            shown = PullRequestsMixin._shown(data.get("description"))
            mismatch = f"'description' to echo the sent description but got {shown}"
        elif reviewers_cleared and data.get("reviewers", []) != []:
            shown = PullRequestsMixin._shown(data.get("reviewers"))
            mismatch = f"'reviewers' to be empty but got {shown}"
        elif sent_version is not None and version != sent_version + 1:
            mismatch = f"a 'version' of {sent_version + 1} but got {version}"
        if mismatch is not None:
            raise ValueError(
                f"Bitbucket returned an unconfirmed pull-request body for {path}; "
                f"expected {mismatch}. The write may have been applied but was "
                "not confirmed."
            )
        return BitbucketPullRequest.from_api_response(data)

    def create_pull_request(
        self,
        project_key: str,
        repository_slug: str,
        title: str,
        from_ref: str,
        to_ref: str,
        *,
        description: str | None = None,
        draft: bool | None = None,
        reviewers: list[str] | None = None,
        from_repo: str | None = None,
    ) -> BitbucketPullRequest:
        """Create a pull request in a Bitbucket Data Center repository.

        Calls ``POST .../pull-requests`` once, with no ref or reviewer lookup
        beforehand. The request body is built by
        :meth:`_build_pull_request_body`: a bare ref name is qualified as
        ``refs/heads/<name>``, a value under ``refs/`` is sent as given,
        ``from_ref`` may be a tag but ``to_ref`` must be a branch, and
        reviewers are sent by user name. The source may live in another
        repository of the same hierarchy (a fork) named by ``from_repo`` as
        ``PROJECT/slug``. The endpoint needs ``REPO_READ`` on both the source
        and the target repository.

        Args:
            project_key: The target project key.
            repository_slug: The target repository slug.
            title: The pull-request title, which must be non-blank.
            from_ref: The source branch or tag.
            to_ref: The target branch.
            description: Optional description (Markdown).
            draft: Optional draft flag. None leaves the server default.
            reviewers: Optional user names to add as reviewers.
            from_repo: Optional ``PROJECT/slug`` of the repository holding
                ``from_ref`` when it is not the target repository.

        Returns:
            The created
            :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`
            (carrying its ``id`` and ``version`` for later writes).

        Raises:
            ValueError: If a segment is blank, ``from_repo`` is malformed, a
                body field fails validation (see
                :meth:`_build_pull_request_body`), the 2xx body does not
                confirm the write (see :meth:`_confirmed_pull_request`), or
                the request fails, where a 400 (malformed entity) or a 409 (a
                reviewer could not be resolved, the refs are the same, the
                target is up to date, a pull request already exists, or the
                target repository is archived) carries the instance's own
                message.
            BitbucketResourceNotFoundError: If a repository or ref does not
                exist or is not accessible. The message names both refs.
            MCPAtlassianAuthenticationError: If the bearer token is rejected,
                or lacks permission on one of the repositories (401/403).
        """
        base = self._pr_base_path(project_key, repository_slug)
        target_repo = (project_key.strip(), repository_slug.strip())
        source_repo: tuple[str, str] | None = None
        if from_repo and from_repo.strip():
            source_repo = self._split_repo_ref(from_repo, name="from_repo")
        body = self._build_pull_request_body(
            title=title,
            description=description,
            draft=draft,
            from_ref=from_ref,
            to_ref=to_ref,
            reviewers=reviewers,
            target_repo=target_repo,
            from_repo=source_repo,
        )
        try:
            data = self._post(base, json_body=body)
        except BitbucketResourceNotFoundError as e:
            # The generic 404 text names the repository and pull request. On
            # a create the missing piece is more often one of the refs.
            source_repo_text = (
                f" in {source_repo[0]}/{source_repo[1]}" if source_repo else ""
            )
            raise BitbucketResourceNotFoundError(
                f"Bitbucket resource not found (HTTP 404) for {base}. The "
                f"repository, the source ref {self._shown(body['fromRef']['id'])}"
                f"{source_repo_text}, or the target ref "
                f"{self._shown(body['toRef']['id'])} does not exist, or the "
                "authenticated user lacks permission to view it."
            ) from e
        return self._confirmed_pull_request(
            data,
            base,
            title=body["title"],
            from_ref_id=body["fromRef"]["id"],
            to_ref_id=body["toRef"]["id"],
            draft=draft,
        )

    def update_pull_request(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        version: int,
        *,
        title: str | None = None,
        description: str | None = None,
        draft: bool | None = None,
        to_ref: str | None = None,
        reviewers: list[str] | None = None,
    ) -> BitbucketPullRequest:
        """Update the metadata of an existing pull request.

        Bitbucket treats an update body as the pull request's new state and
        clears the reviewers (at least) when the body leaves them out, so the
        update reads before it writes: ``GET .../pull-requests/{id}``, then
        one ``PUT`` carrying the optimistic-lock ``version`` and every
        updatable field, each either the change given or the value just read
        (see :meth:`_update_body`). ``None`` means "leave as is" and an empty
        ``description`` clears it. The changes are validated by
        :meth:`_build_pull_request_body` before any request, so ``to_ref`` is
        qualified like a create's target (a bare name becomes
        ``refs/heads/<name>`` and a tag is rejected) and reviewers are sent by
        user name. Sending ``reviewers`` replaces the whole list, so ``[]``
        removes every reviewer. The author and the participants cannot be
        changed here. The endpoint needs ``REPO_WRITE`` on the repository, or
        ``REPO_READ`` when the caller is the pull request's author.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            version: The pull request's current ``version`` (from
                :meth:`get_pull_request`).
            title: A new title, or ``None`` to keep the current one.
            description: A new description (Markdown), or ``None`` to keep
                the current one.
            draft: A new draft flag, or ``None`` to keep the current one.
            to_ref: A new target branch, or ``None`` to keep the current one.
            reviewers: The complete new reviewer list (user names), ``[]`` to
                clear it, or ``None`` to keep the current one.

        Returns:
            The updated
            :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`
            (carrying its new ``version`` for later writes).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                ``version`` is not a non-negative int, no field to change is
                given, a field fails validation (see
                :meth:`_build_pull_request_body`), the pull request read is at
                another version or cannot be re-sent (see
                :meth:`_update_body`; no write is sent), the 2xx body does not
                confirm the write (see :meth:`_confirmed_pull_request`), or
                the request fails, where a 400 or a 409 (a stale version, a
                reviewer that could not be added, a target conflict, or an
                archived repository) carries the instance's own message.
            BitbucketResourceNotFoundError: If the repository, the pull
                request, or the new target branch does not exist or is not
                accessible. With a new target the message names it.
            MCPAtlassianAuthenticationError: If the bearer token is rejected
                or lacks permission to update this pull request (401/403).
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        self._check_version(version)
        if (
            title is None
            and description is None
            and draft is None
            and to_ref is None
            and reviewers is None
        ):
            raise ValueError(
                "Nothing to update: give at least one of title, description, "
                "draft, to_ref, or reviewers."
            )
        target_repo = (project_key.strip(), repository_slug.strip())
        changes = self._build_pull_request_body(
            title=title,
            description=description,
            draft=draft,
            to_ref=to_ref,
            reviewers=reviewers,
            target_repo=target_repo,
        )
        path = f"{base}/{pr_id}"
        body = self._update_body(
            self._get(path),
            changes,
            path=path,
            version=version,
            target_repo=target_repo,
        )
        try:
            data = self._put(path, json_body=body)
        except BitbucketResourceNotFoundError as e:
            if "toRef" not in changes:
                raise
            # The generic 404 text names the repository and pull request. With
            # a new target the missing piece is more often that branch.
            raise BitbucketResourceNotFoundError(
                f"Bitbucket resource not found (HTTP 404) for {path}. The "
                "repository, the pull request, or the new target ref "
                f"{self._shown(body['toRef']['id'])} does not exist, or the "
                "authenticated user lacks permission to view it."
            ) from e
        return self._confirmed_pull_request(
            data,
            path,
            title=changes.get("title"),
            to_ref_id=changes["toRef"]["id"] if "toRef" in changes else None,
            draft=draft,
            description=description,
            reviewers_cleared=reviewers is not None and not reviewers,
            sent_version=version,
            pr_id=pr_id,
        )

    @staticmethod
    def _update_body(
        current: Any,
        changes: dict[str, Any],
        *,
        path: str,
        version: int,
        target_repo: tuple[str, str],
    ) -> dict[str, Any]:
        """Merge the requested changes into the pull request's current state.

        Every updatable field is sent: ``title``, ``toRef``, and ``reviewers``
        always, and ``description`` and ``draft`` when the change or the
        pull request carries them (Bitbucket omits an empty description, and
        a version without draft pull requests omits the flag). A field not in
        ``changes`` takes the value read. Reviewers are re-sent by user name,
        without the ``MAX_PR_REVIEWERS`` cap that applies to a requested
        list, so a pull request that already has more can still be updated.

        Args:
            current: The parsed ``GET .../pull-requests/{id}`` body.
            changes: The validated fields to change, from
                :meth:`_build_pull_request_body`.
            path: The API path, for the error messages.
            version: The optimistic-lock version the caller sent.
            target_repo: The ``(project_key, repository_slug)`` the pull
                request belongs to, for the ``toRef`` repository.

        Returns:
            The ``PUT`` body, carrying ``version``.

        Raises:
            ValueError: If ``current`` is not an object, its ``version`` is
                not an integer or differs from ``version`` (the caller's view
                is stale), or a field to keep is missing or malformed (a title, a target ref
                id, or a reviewer user name), since leaving it out of the
                write could clear it.
        """
        if not isinstance(current, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a pull-request object. Nothing was updated."
            )

        def _missing(field: str) -> ValueError:
            return ValueError(
                f"Bitbucket returned a pull request for {path} without a usable "
                f"{field}. Nothing was updated, because leaving it out of the "
                "update could clear it."
            )

        current_version = current.get("version")
        if not isinstance(current_version, int) or isinstance(current_version, bool):
            raise _missing("'version'")
        if current_version != version:
            raise ValueError(
                f"version {version} is stale: the pull request at {path} is at "
                f"version {current_version}. Read it again with "
                "bitbucket_get_pull_request and reapply the change. Nothing was "
                "updated."
            )

        body = dict(changes)
        if "title" not in body:
            title = current.get("title")
            if not isinstance(title, str) or not title.strip():
                raise _missing("'title'")
            body["title"] = title
        if "description" not in body and isinstance(current.get("description"), str):
            body["description"] = current["description"]
        if "draft" not in body and isinstance(current.get("draft"), bool):
            body["draft"] = current["draft"]
        if "toRef" not in body:
            to_ref = current.get("toRef")
            ref_id = to_ref.get("id") if isinstance(to_ref, dict) else None
            if not isinstance(ref_id, str) or not ref_id:
                raise _missing("'toRef.id'")
            body["toRef"] = PullRequestsMixin._ref_object(ref_id, target_repo)
        if "reviewers" not in body:
            entries = current.get("reviewers")
            if not isinstance(entries, list):
                raise _missing("'reviewers' list")
            names: list[str] = []
            for entry in entries:
                user = entry.get("user") if isinstance(entry, dict) else None
                name = user.get("name") if isinstance(user, dict) else None
                if not isinstance(name, str) or not name:
                    raise _missing("reviewer user name")
                names.append(name)
            body["reviewers"] = [{"user": {"name": name}} for name in names]
        body["version"] = version
        return body

    @staticmethod
    def _check_version(version: int) -> None:
        """Reject a non-integer or negative ``version`` before any request."""
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError(
                "version must be a non-negative integer (the pull request's "
                "current version)."
            )

    @staticmethod
    def _check_optional_text(value: Any, *, name: str) -> None:
        """Reject a blank or oversized optional text field before any request.

        The merge ``message`` and the decline ``comment`` are shipped verbatim.
        Bounding them client-side keeps an oversized body off the instance.
        The cap reuses ``MAX_COMMENT_TEXT_CHARS``, the limit Bitbucket applies
        to a comment (which a decline comment becomes).

        Args:
            value: The text as given.
            name: The parameter name, for the error message.

        Raises:
            ValueError: If the value is not a non-blank string, or exceeds
                ``MAX_COMMENT_TEXT_CHARS``.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-blank string when given.")
        if len(value) > MAX_COMMENT_TEXT_CHARS:
            raise ValueError(
                f"{name} is {len(value)} characters; at most "
                f"{MAX_COMMENT_TEXT_CHARS} are accepted. Shorten it."
            )

    @staticmethod
    def _confirmed_lifecycle_pull_request(
        data: Any, path: str, *, pr_id: int, expected_state: str, sent_version: int
    ) -> BitbucketPullRequest:
        """Parse a 2xx lifecycle-write body, confirming it against the request.

        A merge, decline, or reopen is confirmed only by a ``RestPullRequest``
        body whose ``id`` is the pull request written, whose ``state`` is the
        one the write produces (``MERGED``, ``DECLINED``, or ``OPEN``), and
        whose integer ``version`` is above the one sent, since each of these
        writes bumps it. Any other 2xx body is reported as an error.

        Args:
            data: The parsed JSON body of the write.
            path: The API path, for the error message.
            pr_id: The id of the pull request written.
            expected_state: The state the write produces.
            sent_version: The optimistic-lock version sent.

        Returns:
            The confirmed :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`.

        Raises:
            ValueError: If the body is not an object or disagrees with the
                request on the id, the state, or the version. The write may
                have been applied on the server. The message says so.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a pull-request object."
            )
        returned_id = data.get("id")
        state = data.get("state")
        version = data.get("version")
        mismatch: str | None = None
        if returned_id != pr_id or isinstance(returned_id, bool):
            mismatch = f"'id' {pr_id} but got {PullRequestsMixin._shown(returned_id)}"
        elif state != expected_state:
            mismatch = (
                f"'state' '{expected_state}' but got {PullRequestsMixin._shown(state)}"
            )
        elif (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= sent_version
        ):
            mismatch = (
                f"a 'version' above {sent_version} but got "
                f"{PullRequestsMixin._shown(version)}"
            )
        if mismatch is not None:
            raise ValueError(
                f"Bitbucket returned an unconfirmed pull-request body for {path}; "
                f"expected {mismatch}. The write may have been applied but was "
                "not confirmed."
            )
        return BitbucketPullRequest.from_api_response(data)

    def merge_pull_request(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        version: int,
        message: str | None = None,
        strategy_id: str | None = None,
    ) -> BitbucketPullRequest:
        """Merge an open pull request immediately.

        Calls ``POST .../pull-requests/{id}/merge`` (one request) with the
        optimistic-lock ``version`` in both the query string and the body, plus
        ``message`` and ``strategyId`` when given. ``autoMerge`` is not sent,
        so this method merges now or fails. The server validates ``strategyId``
        against the strategies enabled on the repository. Common ids are
        ``no-ff``, ``ff``, ``ff-only``, ``squash``, ``squash-ff-only``,
        ``rebase-no-ff``, and ``rebase-ff-only``. Needs ``REPO_WRITE`` on the
        target repository.

        No merge-status read precedes the write. Callers check
        :meth:`get_pull_request_merge_status` first and pass the ``version``
        from :meth:`get_pull_request`, so a merge-check veto, a conflict, or a
        stale version is one 409 carrying the instance's own message. On a
        timeout or a 5xx the write may still have been applied, so re-read the
        pull request before retrying.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            version: The pull request's current ``version``.
            message: Text placed below the server's generated subject line in
                the merge commit (the server keeps its ``autoSubject`` default).
                When omitted, the commit carries the subject alone.
            strategy_id: The merge strategy id, or the repository default when
                omitted.

        Returns:
            The merged :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`
            (``state`` ``MERGED``).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                ``version`` is not a non-negative int, ``message`` is blank or
                over ``MAX_COMMENT_TEXT_CHARS``, ``strategy_id`` is blank, the
                2xx body does not confirm the merge (the write may
                have been applied but was not confirmed), or the request fails
                (a 409 for a veto, conflict, stale version, or closed pull
                request carries the instance's own message).
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected or
                lacks ``REPO_WRITE`` (HTTP 401/403, with the instance's message).
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        self._check_version(version)
        body: dict[str, Any] = {"version": version}
        if message is not None:
            self._check_optional_text(message, name="message")
            body["message"] = message
        if strategy_id is not None:
            if not isinstance(strategy_id, str) or not strategy_id.strip():
                raise ValueError("strategy_id must be a non-blank string when given.")
            body["strategyId"] = strategy_id
        path = f"{base}/{pr_id}/merge"
        data = self._post(path, json_body=body, params={"version": version})
        return self._confirmed_lifecycle_pull_request(
            data, path, pr_id=pr_id, expected_state="MERGED", sent_version=version
        )

    def decline_pull_request(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        version: int,
        comment: str | None = None,
    ) -> BitbucketPullRequest:
        """Decline an open pull request.

        Calls ``POST .../pull-requests/{id}/decline`` (one request) with the
        optimistic-lock ``version`` in both the query string and the body, plus
        ``comment`` when given. Needs ``REPO_READ``. A declined pull request can
        be reopened with :meth:`reopen_pull_request`.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            version: The pull request's current ``version`` (from
                :meth:`get_pull_request`).
            comment: An optional comment explaining the decline.

        Returns:
            The declined
            :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`
            (``state`` ``DECLINED``).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                ``version`` is not a non-negative int, ``comment`` is blank or
                over ``MAX_COMMENT_TEXT_CHARS``, the 2xx body does not confirm
                the decline (the write may have been applied
                but was not confirmed), or the request fails (a 409 for a stale
                version or a pull request that is not open carries the
                instance's own message).
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        self._check_version(version)
        body: dict[str, Any] = {"version": version}
        if comment is not None:
            self._check_optional_text(comment, name="comment")
            body["comment"] = comment
        path = f"{base}/{pr_id}/decline"
        data = self._post(path, json_body=body, params={"version": version})
        return self._confirmed_lifecycle_pull_request(
            data, path, pr_id=pr_id, expected_state="DECLINED", sent_version=version
        )

    def reopen_pull_request(
        self,
        project_key: str,
        repository_slug: str,
        pull_request_id: int | str,
        version: int,
    ) -> BitbucketPullRequest:
        """Reopen a declined pull request.

        Calls ``POST .../pull-requests/{id}/reopen`` (one request) with the
        optimistic-lock ``version`` in both the query string and the body.
        Needs ``REPO_READ``. A merged pull request cannot be reopened, and the
        server answers 409.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            pull_request_id: The pull-request id (positive integer).
            version: The pull request's current ``version`` (from
                :meth:`get_pull_request`).

        Returns:
            The reopened
            :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequest`
            (``state`` ``OPEN``).

        Raises:
            ValueError: If a segment is blank, the id is not a positive integer,
                ``version`` is not a non-negative int, the 2xx body does not
                confirm the reopen (the write may have been applied but was
                not confirmed), or the request fails (a 409 for a stale
                version or a pull request that is not declined carries the
                instance's own message).
            BitbucketResourceNotFoundError: If the pull request does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._pr_base_path(project_key, repository_slug)
        pr_id = self._coerce_pr_id(pull_request_id)
        self._check_version(version)
        path = f"{base}/{pr_id}/reopen"
        data = self._post(
            path, json_body={"version": version}, params={"version": version}
        )
        return self._confirmed_lifecycle_pull_request(
            data, path, pr_id=pr_id, expected_state="OPEN", sent_version=version
        )
