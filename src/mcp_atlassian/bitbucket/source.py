"""Bitbucket Data Center source / file browse operations.

The ``browse`` endpoint serves both a directory's child listing and a file's
windowed text lines from one URL: a path that resolves to a directory returns a
paged ``children`` listing, a path that resolves to a file returns paged
``lines``. This mixin encodes the caller path safely (slashes preserved,
traversal rejected), issues one bounded request, and discriminates the response
shape into a :class:`BitbucketBrowseResult`.
"""

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..models.bitbucket import BitbucketDirectoryEntry
from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Default number of lines (file) or children (directory) a browse call returns.
DEFAULT_BROWSE_LIMIT = 100
# Hard ceiling on the per-call browse window (the tool's ``limit``); the caller
# pages further with the cursor. Caps token/line volume from one request.
MAX_BROWSE_LIMIT = 1000


@dataclass
class BitbucketBrowseResult:
    """A single bounded window of a ``browse`` response (file or directory).

    Exactly one of ``lines``/``children`` is populated, per ``kind``.

    Attributes:
        kind: ``"FILE"`` or ``"DIRECTORY"`` (the discriminated shape). For an
            unrecognised body it is ``"FILE"`` with empty ``lines`` (a defensive
            empty result, never a raise).
        path: The caller-supplied path (echoed back; empty for the repo root).
        lines: The file's text lines for this window, or None for a directory.
        children: The directory's child entries, or None for a file.
        is_last_page: Whether this window reached the end of the upstream list.
        next_page_start: The ``start`` cursor to resume from, or None when the
            window is the last page.
        truncated: Whether more content exists beyond this window
            (``not is_last_page``).
        binary: Whether the file is binary (file bodies only; False otherwise).
    """

    kind: str
    path: str
    lines: list[str] | None
    children: list[BitbucketDirectoryEntry] | None
    is_last_page: bool
    next_page_start: int | None
    truncated: bool
    binary: bool


class SourceMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center source / file browse operations."""

    @staticmethod
    def _encode_browse_path(path: str) -> str:
        """Percent-encode a caller browse path, rejecting traversal.

        The security-critical piece: this is the only place caller path text
        reaches the URL. The path is split on ``/`` and **any empty, ``.``, or
        ``..`` component is rejected** (no traversal, no doubled or leading/
        trailing slashes), then each surviving component is percent-encoded with
        ``quote(safe="")`` (so a component can never inject an unescaped slash or
        query separator) and the components are rejoined with ``/`` — real path
        separators are preserved, but only between validated components.

        Args:
            path: The caller-supplied path. An empty/blank value means the repo
                root and yields an empty string (no ``{path}`` suffix).

        Returns:
            The encoded path (slashes preserved between components), or an empty
            string for the repo root.

        Raises:
            ValueError: If any component is empty, ``.``, or ``..`` — caught
                here, before any request is issued.
        """
        if path is None or not path.strip():
            return ""
        components = path.split("/")
        encoded: list[str] = []
        for component in components:
            if component in ("", ".", ".."):
                raise ValueError(
                    "Invalid path component in browse path: empty, '.', and "
                    "'..' segments are not allowed (path traversal rejected)."
                )
            encoded.append(quote(component, safe=""))
        return "/".join(encoded)

    def browse(
        self,
        project_key: str,
        repository_slug: str,
        *,
        path: str = "",
        at: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_BROWSE_LIMIT,
    ) -> BitbucketBrowseResult:
        """Browse a path in a Bitbucket Data Center repository.

        Calls ``GET .../browse`` (repo root) or ``GET .../browse/{path}`` once.
        A directory path returns a paged child listing; a file path returns a
        windowed slice of text lines. The response shape is discriminated on the
        body (``children`` → directory, ``lines`` → file). This is a dedicated
        single-window read: the ``browse`` envelope is not the ``values`` shape
        the shared paginator validates, so it issues exactly one upstream
        request and surfaces the cursor for resumption (never a client walk).

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            path: The path to browse (file or directory). Empty/blank means the
                repository root. ``.``/``..``/empty components are rejected.
            at: Optional commit id, branch, or tag ref to read at; the default
                branch is used when omitted.
            start: The 0-based line/child offset to resume from (the
                ``next_page_start`` of a prior call). 0 starts from the
                beginning.
            limit: Maximum number of lines (file) or children (directory) to
                return in this window. Clamped to ``[1, MAX_BROWSE_LIMIT]``.

        Returns:
            A :class:`BitbucketBrowseResult` carrying either ``lines`` (file) or
            ``children`` (directory), the ``is_last_page``/``next_page_start``
            cursor, ``truncated``, and ``binary``.

        Raises:
            ValueError: If a segment is blank, a path component is invalid, the
                response is not a JSON object, or the request fails.
            BitbucketResourceNotFoundError: If the repository or path does not
                exist or is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._repo_base_path(project_key, repository_slug)
        enc = self._encode_browse_path(path)
        url = f"{base}/browse" + (f"/{enc}" if enc else "")
        limit = max(1, min(limit, MAX_BROWSE_LIMIT))
        params: dict[str, Any] = {"start": start, "limit": limit}
        if at and at.strip():
            params["at"] = at.strip()
        # One upstream request: the browse envelope is not the `values` shape the
        # shared paginator walks, so this is a dedicated single-window read.
        data = self._get(url, params=params)
        if not isinstance(data, dict):
            msg = (
                "Bitbucket returned an unexpected response shape for "
                f"{url}; expected a browse object."
            )
            raise ValueError(msg)
        return self._build_result(data, path)

    @staticmethod
    def _build_result(data: dict[str, Any], path: str) -> BitbucketBrowseResult:
        """Discriminate a browse body into a :class:`BitbucketBrowseResult`.

        A body with ``children`` is a directory; one with ``lines`` is a file.
        A body with neither (an unrecognised/empty shape) yields an empty file
        result and logs a warning — a legitimately empty file or directory never
        raises.

        Args:
            data: The browse response body (a JSON object).
            path: The caller-supplied path, echoed into the result.

        Returns:
            A :class:`BitbucketBrowseResult`.
        """
        children = data.get("children")
        lines = data.get("lines")
        if isinstance(children, dict):
            raw_values = children.get("values")
            values = raw_values if isinstance(raw_values, list) else []
            entries = [
                BitbucketDirectoryEntry.from_api_response(v)
                for v in values
                if isinstance(v, dict)
            ]
            is_last_page = bool(children.get("isLastPage", True))
            next_page_start = children.get("nextPageStart")
            return BitbucketBrowseResult(
                kind="DIRECTORY",
                path=path,
                lines=None,
                children=entries,
                is_last_page=is_last_page,
                next_page_start=next_page_start if not is_last_page else None,
                truncated=not is_last_page,
                binary=False,
            )
        if "children" in data:
            # A present-but-non-dict `children` (e.g. a list from a drifted or
            # malformed response) must not fall through and be misclassified as
            # an empty FILE — surface it as a shape-drift result instead.
            logger.warning(
                "Unexpected browse response shape: 'children' present but is "
                "%s, not an object; returning an empty result.",
                type(children).__name__,
            )
            return BitbucketBrowseResult(
                kind="FILE",
                path=path,
                lines=[],
                children=None,
                is_last_page=True,
                next_page_start=None,
                truncated=False,
                binary=False,
            )
        if isinstance(lines, list):
            binary = bool(data.get("binary"))
            # Binary files: the `binary` flag is the signal — never extract or
            # forward the raw byte payload that may ride in `lines[].text`.
            text_lines = (
                []
                if binary
                else [
                    line.get("text") or "" for line in lines if isinstance(line, dict)
                ]
            )
            is_last_page = bool(data.get("isLastPage", True))
            next_page_start = data.get("nextPageStart")
            return BitbucketBrowseResult(
                kind="FILE",
                path=path,
                lines=text_lines,
                children=None,
                is_last_page=is_last_page,
                next_page_start=next_page_start if not is_last_page else None,
                truncated=not is_last_page,
                binary=binary,
            )
        logger.warning(
            "Unexpected browse response shape: neither 'children' nor 'lines' "
            "present; returning an empty result."
        )
        return BitbucketBrowseResult(
            kind="FILE",
            path=path,
            lines=[],
            children=None,
            is_last_page=True,
            next_page_start=None,
            truncated=False,
            binary=False,
        )
