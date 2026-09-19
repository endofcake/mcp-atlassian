"""Bitbucket Data Center branch and tag (ref) operations."""

import logging
import re
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

# Bitbucket Data Center branch-utils REST module base path. Branch delete
# lives here rather than under the core API (see :meth:`RefsMixin.delete_branch`).
BRANCH_UTILS_BASE_PATH = "/rest/branch-utils/1.0"

# A full git commit id (a 40-character hexadecimal SHA-1).
_FULL_COMMIT_ID = re.compile(r"^[0-9a-fA-F]{40}$")
# A hexadecimal string of any length, the shape ``latestCommit`` takes.
_HEX = re.compile(r"^[0-9a-fA-F]+$")
# Characters git forbids anywhere in a ref name: ASCII space and control
# characters (including DEL), ``~``, ``^``, ``:``, ``?``, ``*``, ``[``, and
# ``\``. Only ASCII whitespace is listed, as in ``git check-ref-format``.
_REF_NAME_FORBIDDEN = re.compile(r"[ ~^:?*\[\\\x00-\x1f\x7f]")
# Cap on the ``message`` sent with a branch or tag create, matching the
# pull-request comment cap, so one write cannot push an oversized payload.
MAX_REF_MESSAGE_CHARS = 32768
# Cap on an upstream value quoted in a confirmation error message.
_SHOWN_MAX_CHARS = 64


def _shown(value: Any) -> str:
    """Return ``repr(value)`` capped for an error message.

    The value comes from the upstream body, so it is bounded before it is
    interpolated into a message that is logged and returned to the client.
    """
    shown = repr(value)
    return shown[:_SHOWN_MAX_CHARS] + "..." if len(shown) > _SHOWN_MAX_CHARS else shown


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

    @staticmethod
    def _validate_ref_name(name: str, *, what: str, allow_refs_prefix: bool) -> str:
        """Validate a caller-supplied branch or tag name as a git ref name.

        Applies the ``git check-ref-format`` rules that are cheap to check
        client-side so an obviously invalid name is rejected before any
        request is issued: non-empty after stripping, not the single
        character ``@``, no ``..``, no ``@{``, no leading ``-``, no leading
        or trailing ``/`` and no empty component, no component starting
        with ``.`` or ending in ``.lock``, no trailing ``.``, and none of the
        characters git forbids (space, ``~``, ``^``, ``:``, ``?``, ``*``,
        ``[``, ``\\``, control characters). The server validates further
        (for example the name length) and its 400 message is passed on as-is.

        Args:
            name: The caller-supplied name.
            what: The human-readable kind of value (e.g. ``"branch name"``).
            allow_refs_prefix: Whether a fully-qualified ``refs/...`` id is
                accepted. Create rejects it because the server prepends
                ``refs/heads/`` or ``refs/tags/`` itself.

        Returns:
            The stripped name.

        Raises:
            ValueError: If the name breaks any of the rules above.
        """
        text = name.strip()
        if not text:
            raise ValueError(f"name must be a non-empty Bitbucket {what}.")
        if not allow_refs_prefix and text.startswith("refs/"):
            raise ValueError(
                f"name must be a short {what} such as 'feature/x'. The server "
                "adds the 'refs/' prefix."
            )
        components = text.split("/")
        if (
            text == "@"
            or ".." in text
            or "@{" in text
            or text.startswith("-")
            or text.startswith("/")
            or text.endswith("/")
            or text.endswith(".")
            or "//" in text
            or _REF_NAME_FORBIDDEN.search(text)
            or any(
                part.startswith(".") or part.endswith(".lock") for part in components
            )
        ):
            raise ValueError(
                f"name {_shown(text)} is not a valid git {what}: it must not "
                "contain '..', '@{', spaces, or the characters ~ ^ : ? * [ \\, "
                "must not be '@', start with '-' or '/', end with '/' or '.', "
                "or have an empty component or a component starting with '.' "
                "or ending in '.lock'."
            )
        return text

    @staticmethod
    def _bounded_message(message: str) -> str:
        """Return ``message`` once it is within ``MAX_REF_MESSAGE_CHARS``.

        Raises:
            ValueError: If the message is longer than the cap. Raised before
                any request is issued.
        """
        if len(message) > MAX_REF_MESSAGE_CHARS:
            raise ValueError(
                f"message must be at most {MAX_REF_MESSAGE_CHARS} characters."
            )
        return message

    @staticmethod
    def _validate_commit_id(value: str, *, name: str) -> str:
        """Validate a caller-supplied full commit id (40 hexadecimal characters).

        Args:
            value: The caller-supplied commit id.
            name: The parameter name used in the error message.

        Returns:
            The stripped commit id.

        Raises:
            ValueError: If the value is not a 40-character hexadecimal string.
                Raised before any request is issued.
        """
        text = value.strip()
        if not _FULL_COMMIT_ID.match(text):
            raise ValueError(
                f"{name} must be a full 40-character hexadecimal commit id, as "
                "returned by list_branches, list_tags, or get_commit."
            )
        return text

    @staticmethod
    def _confirm_created_ref(
        data: Any, path: str, *, name: str, start_point: str, what: str
    ) -> dict[str, Any]:
        """Require a ref create's 2xx body to acknowledge the request.

        A branch or tag create is confirmed only by a ref object whose
        ``displayId`` equals the sent name and whose ``latestCommit`` is a
        hexadecimal commit id. When the sent ``startPoint`` was itself a full
        commit id, ``latestCommit`` must equal it. Any other 2xx body is
        reported as an error, so no ref is built from an unverified name or
        commit.

        Args:
            data: The parsed JSON body of the write.
            path: The API path, for the error message.
            name: The short ref name that was sent.
            start_point: The ``startPoint`` that was sent.
            what: The human-readable kind of ref (e.g. ``"branch"``).

        Returns:
            The body, as a dict, once confirmed.

        Raises:
            ValueError: If the body is not an object, its ``displayId``
                differs from the sent name, or its ``latestCommit`` is not a
                hexadecimal string (equal to ``start_point`` when that was a
                full commit id). The write may have been applied on the
                server. The message says so.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a {what} object."
            )
        display_id = data.get("displayId")
        latest_commit = data.get("latestCommit")
        mismatch: str | None = None
        if display_id != name:
            mismatch = f"'displayId' {name!r} but got {_shown(display_id)}"
        elif not isinstance(latest_commit, str) or not _HEX.match(latest_commit):
            mismatch = f"a hexadecimal 'latestCommit' but got {_shown(latest_commit)}"
        elif (
            _FULL_COMMIT_ID.match(start_point)
            and latest_commit.lower() != start_point.lower()
        ):
            mismatch = f"'latestCommit' {start_point!r} but got {_shown(latest_commit)}"
        if mismatch is not None:
            raise ValueError(
                f"Bitbucket returned an unconfirmed {what} body for {path}; "
                f"expected {mismatch}. The write may have been applied but was "
                "not confirmed."
            )
        return data

    def create_branch(
        self,
        project_key: str,
        repository_slug: str,
        name: str,
        start_point: str,
        *,
        message: str | None = None,
    ) -> BitbucketBranch:
        """Create a branch in a Bitbucket Data Center repository.

        Calls ``POST /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        branches`` with ``{name, startPoint, message?}``. The server prepends
        ``refs/heads/`` to the short name. The endpoint requires
        **REPO_WRITE** on the repository. One request per call. The start
        point is not resolved beforehand.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            name: The short branch name (e.g. ``"feature/x"``), without a
                ``refs/`` prefix.
            start_point: The commit id or ref the branch starts from (e.g.
                ``"main"`` or a 40-character commit id).
            message: Optional message recorded with the ref change (at most
                ``MAX_REF_MESSAGE_CHARS`` characters).

        Returns:
            The created :class:`~mcp_atlassian.models.bitbucket.BitbucketBranch`,
            confirmed against the request (see :meth:`_confirm_created_ref`).

        Raises:
            ValueError: If a segment is blank, the name is not a valid git
                branch name, the start point is blank, the message exceeds
                the cap, the 2xx body does not confirm the request, or the
                request fails (a 400/409, for example a name that already
                exists, passes on the instance's own message).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected or
                lacks REPO_WRITE (401).
        """
        path = f"{self._repo_base_path(project_key, repository_slug)}/branches"
        short_name = self._validate_ref_name(
            name, what="branch name", allow_refs_prefix=False
        )
        start = start_point.strip()
        if not start:
            raise ValueError("start_point must be a non-empty commit id or ref.")
        body: dict[str, Any] = {"name": short_name, "startPoint": start}
        if message is not None and message.strip():
            body["message"] = self._bounded_message(message)
        data = self._post(path, json_body=body)
        confirmed = self._confirm_created_ref(
            data, path, name=short_name, start_point=start, what="branch"
        )
        return BitbucketBranch.from_api_response(confirmed)

    def delete_branch(
        self,
        project_key: str,
        repository_slug: str,
        name: str,
        end_point: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete a branch in a Bitbucket Data Center repository.

        Calls ``DELETE /rest/branch-utils/1.0/projects/{projectKey}/repos/
        {repositorySlug}/branches`` with ``{name, endPoint, dryRun}``. The
        delete is conditional: ``endPoint`` must be the commit the branch
        currently points at, so a branch that has moved since the caller read
        it is not deleted (the server answers 400 when it points elsewhere).
        A short name is sent as ``refs/heads/<name>`` so the request cannot
        resolve to a tag. A fully-qualified ``refs/heads/...`` id is sent as
        given. HTTP 204
        acknowledges the request even when the branch did not exist, so the
        result reports the request as ``accepted`` with a ``note``. List
        branches afterwards to verify the resulting state. The endpoint
        requires **REPO_WRITE** (and branch permission on the name).

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            name: The branch name, short (``"feature/x"``) or fully qualified
                (``"refs/heads/feature/x"``).
            end_point: The full 40-character commit id the branch is expected
                to point at (its ``latest_commit`` from :meth:`list_branches`).
            dry_run: When True the server validates the request but deletes
                nothing.

        Returns:
            A dict with the fully-qualified ``branch`` id that was sent, the
            ``end_point``, ``dry_run``, ``accepted`` (True: the server
            answered 204), and a ``note`` on verifying the result.

        Raises:
            ValueError: If a segment is blank, the name is not a valid git
                branch name, ``end_point`` is not a full commit id, the 2xx
                response carries a body, or the request fails (a 400, for
                example the branch pointing elsewhere or being the default
                branch, passes on the instance's own message).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected or
                lacks REPO_WRITE or branch permission (401).
        """
        path = f"{self._repo_base_path(project_key, repository_slug)}/branches"
        ref_name = self._validate_ref_name(
            name, what="branch name", allow_refs_prefix=True
        )
        if ref_name.startswith("refs/") and not ref_name.startswith("refs/heads/"):
            raise ValueError(
                "name must be a branch: a short name such as 'feature/x' or a "
                "'refs/heads/...' id, not another 'refs/' namespace."
            )
        if not ref_name.startswith("refs/heads/"):
            ref_name = f"refs/heads/{ref_name}"
        commit_id = self._validate_commit_id(end_point, name="end_point")
        body = {"name": ref_name, "endPoint": commit_id, "dryRun": bool(dry_run)}
        self._delete(path, json_body=body, base_path=BRANCH_UTILS_BASE_PATH)
        note = (
            "Dry run: nothing was deleted."
            if dry_run
            else (
                "Bitbucket answered 204, which guarantees the branch no longer "
                "exists but not that it existed. Verify with list_branches."
            )
        )
        return {
            "branch": ref_name,
            "end_point": commit_id,
            "dry_run": bool(dry_run),
            "accepted": True,
            "note": note,
        }

    def create_tag(
        self,
        project_key: str,
        repository_slug: str,
        name: str,
        start_point: str,
        *,
        message: str | None = None,
    ) -> BitbucketTag:
        """Create a tag in a Bitbucket Data Center repository.

        Calls ``POST /rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/
        tags`` with ``{name, startPoint, message?}``. The server prepends
        ``refs/tags/`` to the short name. A ``message`` makes an annotated
        tag. Without one the tag is lightweight. The endpoint requires
        **REPO_WRITE** on the repository. One request per call.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            name: The short tag name (e.g. ``"v1.2.0"``), without a ``refs/``
                prefix.
            start_point: The commit id or ref the tag points at.
            message: Optional annotation message (makes an annotated tag; at
                most ``MAX_REF_MESSAGE_CHARS`` characters).

        Returns:
            The created :class:`~mcp_atlassian.models.bitbucket.BitbucketTag`,
            confirmed against the request (see :meth:`_confirm_created_ref`).

        Raises:
            ValueError: If a segment is blank, the name is not a valid git tag
                name, the start point is blank, the message exceeds the cap,
                the 2xx body does not confirm the request, or the request
                fails (a 400/409, for example a name that already exists,
                passes on the instance's own message).
            BitbucketResourceNotFoundError: If the repository does not exist or
                is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected or
                lacks REPO_WRITE (401).
        """
        path = f"{self._repo_base_path(project_key, repository_slug)}/tags"
        short_name = self._validate_ref_name(
            name, what="tag name", allow_refs_prefix=False
        )
        start = start_point.strip()
        if not start:
            raise ValueError("start_point must be a non-empty commit id or ref.")
        body: dict[str, Any] = {"name": short_name, "startPoint": start}
        if message is not None and message.strip():
            body["message"] = self._bounded_message(message)
        data = self._post(path, json_body=body)
        confirmed = self._confirm_created_ref(
            data, path, name=short_name, start_point=start, what="tag"
        )
        return BitbucketTag.from_api_response(confirmed)
