"""Bitbucket Data Center pull-request operations."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..models.bitbucket import (
    BitbucketActivity,
    BitbucketComment,
    BitbucketPullRequest,
    BitbucketPullRequestDiff,
    BitbucketUser,
)
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# --- list_pull_requests bounds ---
# Default number of pull requests a single list_pull_requests call returns.
DEFAULT_PRS_LIMIT = 25
# Hard ceiling on pull requests returned in one call. This caps the per-call
# window (the tool's `limit`); the caller pages further with the returned cursor.
MAX_PRS_LIMIT = 100

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
    :class:`~mcp_atlassian.bitbucket.client.BitbucketProjectsPage`). When an
    ``action`` filter is applied, an empty ``truncated`` result means the scan
    did not reach a matching entry, not that none exists.

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
        start: int = 0,
        limit: int = DEFAULT_PRS_LIMIT,
    ) -> BitbucketPullRequestsPage:
        """List pull requests in a Bitbucket Data Center repository.

        Calls ``GET /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        pull-requests`` (paged with ``start``/``limit``), fetching a single
        window (``start`` → up to ``limit``) in one request and returning the
        upstream cursor as ``next_page_start`` so the caller can resume.

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
        # Single window: one upstream request, cursor surfaced for resumption.
        page = self._paginate(
            path,
            limit=limit,
            page_size=limit,
            max_pages=1,
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
        start: int = 0,
        limit: int = DEFAULT_ACTIVITIES_LIMIT,
    ) -> BitbucketActivitiesPage:
        """List a pull request's activity timeline (comments + approvals + …).

        Calls ``GET .../pull-requests/{pullRequestId}/activities`` (paged). With
        no ``action`` this fetches a single window (``start`` → up to ``limit``)
        in one request. The endpoint exposes no action query filter, so an
        ``action`` is applied client-side: each window is then filled by a
        bounded server-side walk (capped by a hard page limit), the mechanism
        behind the comments view (``action="COMMENTED"``). Either way the
        upstream cursor is returned as ``next_page_start`` so the caller can
        resume; under an ``action`` filter the returned ``count`` is post-filter,
        so a window can be empty while more pages remain — keep paging with
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
        limit = max(1, min(limit, MAX_ACTIVITIES_LIMIT))

        # Filtered: a bounded server-side walk per window keeps the per-call
        # request cap. Unfiltered: a single window (one upstream request) with
        # the cursor surfaced for resumption.
        if action is not None:
            wanted = action.strip().upper()

            def _filter(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [v for v in values if str(v.get("action", "")).upper() == wanted]

            transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = (
                _filter
            )
            page_size = _ACTIVITIES_PAGE_SIZE
            max_pages = _MAX_ACTIVITY_PAGES
        else:
            transform = None
            page_size = limit
            max_pages = 1

        page = self._paginate(
            f"{base}/{pr_id}/activities",
            limit=limit,
            page_size=page_size,
            max_pages=max_pages,
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

        No ``diffType``/``fromHash``/``toHash`` is ever emitted, so the server
        resolves an anchor against the PR's **EFFECTIVE** diff — the same frame
        ``get_pull_request_diff`` reads, so no commit-hash bookkeeping is needed.

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
                mode params are combined illegally (see the design's validation
                matrix).
        """
        if not text or not text.strip():
            raise ValueError("text must be a non-blank comment string.")

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
                the mode params are combined illegally, or the request fails
                (a 400/409 surfaces the instance's own message).
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
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{base}/{pr_id}/comments; expected a comment object."
            )
        return BitbucketComment.from_api_response(data)

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
        status — that rejection surfaces with the server's own message. On Data
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
                be resolved, or the request fails (a 400/409 — e.g. the author
                setting a status — surfaces the instance's own message).
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
        path = f"{base}/{pr_id}/participants/{quote(user_slug, safe='')}"
        data = self._put(path, json_body={"status": normalized})
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a participant object."
            )

        # Project explicitly — never echo the raw participant body.
        participant: dict[str, Any] = {"status": data.get("status")}
        if "approved" in data:
            participant["approved"] = data.get("approved")
        user_data = data.get("user")
        if isinstance(user_data, dict):
            participant["user"] = BitbucketUser.from_api_response(
                user_data
            ).to_simplified_dict()
        return participant
