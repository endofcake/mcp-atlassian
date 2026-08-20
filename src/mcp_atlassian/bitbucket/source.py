"""Bitbucket Data Center source / file browse operations.

The ``browse`` endpoint serves both a directory's child listing and a file's
windowed text lines from one URL: a path that resolves to a directory returns a
paged ``children`` listing, and a path that resolves to a file returns paged
``lines``. A binary file's body carries only ``{binary, path}``. This mixin
encodes the caller path (slashes preserved, traversal rejected), issues one
bounded request, and discriminates the response shape into a
:class:`BitbucketBrowseResult`.
"""

from dataclasses import dataclass
from typing import Any

from ..models.bitbucket import BitbucketDirectoryEntry
from ..utils.pagination import clamp_limit
from .client import BitbucketClient

# Default number of lines (file) or children (directory) a browse call returns.
DEFAULT_BROWSE_LIMIT = 100
# Hard ceiling on the per-call browse window (the tool's ``limit``); the caller
# pages further with the cursor. Caps token/line volume from one request. The
# result builder slices any overshoot, so the ceiling holds even when the
# server ignores ``limit``.
MAX_BROWSE_LIMIT = 1000
# Cap on the decoded bytes downloaded for one browse window. The line limit
# bounds only the number of lines, so a minified or generated file could
# return a very large body inside the line limit. A window of 1000 lines of
# ordinary source stays well under this cap.
MAX_BROWSE_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass
class BitbucketBrowseResult:
    """A single bounded window of a ``browse`` response (file or directory).

    Exactly one of ``lines``/``children`` is populated, per ``kind``.

    Attributes:
        kind: ``"FILE"`` or ``"DIRECTORY"`` (the discriminated shape). A body
            matching neither shape raises ``ValueError`` in ``browse``; only a
            completely empty body (``{}``) is reported as an empty ``"FILE"``.
        path: The caller-supplied path (echoed back; empty for the repo root).
        lines: The file's text lines for this window, or None for a directory.
        children: The directory's child entries, or None for a file.
        is_last_page: Whether this window reached the end of the upstream list.
        next_page_start: The ``start`` cursor to resume from, or None when no
            cursor is advertised (the cursor rule is
            :meth:`BitbucketClient._page_cursor`, the overshoot rule
            :meth:`SourceMixin._window`).
        truncated: Whether content was omitted from this window, because
            more exists upstream or because a surplus was trimmed (see
            :meth:`SourceMixin._window`).
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

        This is the only place caller path text reaches the browse URL; see
        :meth:`BitbucketClient._encode_repo_path` for the component rules.

        Args:
            path: The caller-supplied path. An empty/blank value means the repo
                root and yields an empty string (no ``{path}`` suffix).

        Returns:
            The encoded path (slashes preserved between components), or an empty
            string for the repo root.

        Raises:
            ValueError: If any component is empty, ``.``, or ``..``. Raised
                before any request is issued.
        """
        return BitbucketClient._encode_repo_path(path, what="browse path")

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
        body (``children`` → directory, ``lines`` → file, a bare ``binary``
        flag → binary file). The ``browse`` envelope differs from the
        ``values`` envelope :meth:`BitbucketClient._fetch_page` reads, so this
        is a dedicated single-window read, with the cursor validated by
        :meth:`BitbucketClient._page_cursor`.

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
                return in this window. Clamped to ``[1, MAX_BROWSE_LIMIT]`` and
                enforced on the result even if the server returns more.

        Returns:
            A :class:`BitbucketBrowseResult` carrying either ``lines`` (file) or
            ``children`` (directory), the ``is_last_page``/``next_page_start``
            cursor, ``truncated``, and ``binary``.

        Raises:
            ValueError: If a segment is blank, a path component is invalid, the
                response is not a JSON object or matches no recognised browse
                shape, the body exceeds ``MAX_BROWSE_RESPONSE_BYTES``, or the
                request fails.
            BitbucketResourceNotFoundError: If the repository or path does not
                exist or is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._repo_base_path(project_key, repository_slug)
        enc = self._encode_browse_path(path)
        url = f"{base}/browse" + (f"/{enc}" if enc else "")
        limit = max(
            1, min(clamp_limit(limit, context="bitbucket.browse"), MAX_BROWSE_LIMIT)
        )
        params: dict[str, Any] = {"start": start, "limit": limit}
        if at and at.strip():
            params["at"] = at.strip()
        data = self._get(
            url, params=params, max_response_bytes=MAX_BROWSE_RESPONSE_BYTES
        )
        if not isinstance(data, dict):
            msg = (
                "Bitbucket returned an unexpected response shape for "
                f"{url}; expected a browse object."
            )
            raise ValueError(msg)
        return self._build_result(data, path, start=start, limit=limit)

    @staticmethod
    def _window(
        items: list[Any], limit: int, *, is_last_page: bool, next_page_start: int | None
    ) -> tuple[list[Any], bool, int | None]:
        """Bound one validated browse window to ``limit``.

        Takes the completion flag and cursor already validated by
        :meth:`BitbucketClient._page_cursor` and applies the overshoot rule of
        :meth:`BitbucketClient._fetch_page`: a server answering with more
        items than requested has its surplus trimmed, the result is marked
        truncated, and no cursor is advertised.

        Args:
            items: The window's items (lines or children).
            limit: The effective per-call limit.
            is_last_page: The validated ``isLastPage`` flag.
            next_page_start: The validated resume cursor, or None.

        Returns:
            The bounded items, the ``truncated`` flag, and the resume cursor.
        """
        overshot = len(items) > limit
        truncated = overshot or not is_last_page
        return items[:limit], truncated, None if overshot else next_page_start

    @staticmethod
    def _build_result(
        data: dict[str, Any],
        path: str,
        *,
        start: int = 0,
        limit: int = MAX_BROWSE_LIMIT,
    ) -> BitbucketBrowseResult:
        """Discriminate a browse body into a :class:`BitbucketBrowseResult`.

        A body with ``children`` is a directory; one with a truthy ``binary``
        flag is a binary file (its ``lines``, if any, are not read); one with
        ``lines`` is a text file. A non-empty body matching none of these is
        malformed and raises, as the paged-envelope checks in ``_page_values``
        do. Only a completely empty body (``{}``) is accepted as an empty
        file.

        Args:
            data: The browse response body (a JSON object).
            path: The caller-supplied path, echoed into the result.
            start: The offset this window was requested at; a resume cursor
                must advance past it (see :meth:`BitbucketClient._page_cursor`).
            limit: The effective per-call limit the lines or children are
                bounded to (see :meth:`_window`).

        Returns:
            A :class:`BitbucketBrowseResult`.

        Raises:
            ValueError: If the body is non-empty but matches no recognised
                browse shape (e.g. a ``children`` value that is not an object,
                a ``lines`` value that is neither null nor a list, or a body
                with neither
                ``children`` nor ``lines`` nor a ``binary`` flag), or if a
                nested entry is malformed: a
                ``children.values`` that is not a list of objects, or a text
                file ``lines`` entry that is not an object with a string
                ``text``.
        """
        children = data.get("children")
        lines = data.get("lines")
        if isinstance(children, dict):
            raw_values = children.get("values")
            if not isinstance(raw_values, list):
                raise ValueError(
                    "Bitbucket returned an unexpected browse response shape for "
                    f"'{path}': 'children.values' is "
                    f"{type(raw_values).__name__}, not a list. Refusing to "
                    "report the malformed body as an empty directory."
                )
            if not all(isinstance(v, dict) for v in raw_values):
                raise ValueError(
                    "Bitbucket returned an unexpected browse response shape for "
                    f"'{path}': expected every 'children.values' entry to be an "
                    "object. Refusing to drop malformed directory entries."
                )
            entries = [BitbucketDirectoryEntry.from_api_response(v) for v in raw_values]
            is_last_page, next_page_start = BitbucketClient._page_cursor(
                children, start=start, path=f"'{path}'"
            )
            entries, truncated, next_page_start = SourceMixin._window(
                entries,
                limit,
                is_last_page=is_last_page,
                next_page_start=next_page_start,
            )
            return BitbucketBrowseResult(
                kind="DIRECTORY",
                path=path,
                lines=None,
                children=entries,
                is_last_page=is_last_page,
                next_page_start=next_page_start,
                truncated=truncated,
                binary=False,
            )
        if "children" in data:
            # A present-but-non-dict `children` (e.g. a list from a drifted or
            # malformed response) would otherwise fall through and be
            # classified as an empty FILE.
            raise ValueError(
                "Bitbucket returned an unexpected browse response shape for "
                f"'{path}': 'children' is {type(children).__name__}, not an "
                "object. Refusing to report the malformed body as an empty "
                "result."
            )
        if data.get("binary"):
            # Binary files: the `binary` flag is the signal, and the raw byte
            # payload that may ride in `lines[].text` is not extracted. A
            # binary body carries only `binary` and `path` and no cursor keys,
            # so a missing `isLastPage` is the last page here. A present flag
            # is validated like any other page.
            is_last_page, next_page_start = True, None
            if "isLastPage" in data:
                is_last_page, next_page_start = BitbucketClient._page_cursor(
                    data, start=start, path=f"'{path}'"
                )
            return BitbucketBrowseResult(
                kind="FILE",
                path=path,
                lines=[],
                children=None,
                is_last_page=is_last_page,
                next_page_start=next_page_start,
                truncated=not is_last_page,
                binary=True,
            )
        if isinstance(lines, list):
            text_lines: list[str] = []
            for line in lines:
                text = line.get("text") if isinstance(line, dict) else None
                if not isinstance(text, str):
                    raise ValueError(
                        "Bitbucket returned an unexpected browse response "
                        f"shape for '{path}': expected every 'lines' entry "
                        "to be an object with a string 'text'. Refusing to "
                        "report the malformed body as file content."
                    )
                text_lines.append(text)
            is_last_page, next_page_start = BitbucketClient._page_cursor(
                data, start=start, path=f"'{path}'"
            )
            text_lines, truncated, next_page_start = SourceMixin._window(
                text_lines,
                limit,
                is_last_page=is_last_page,
                next_page_start=next_page_start,
            )
            return BitbucketBrowseResult(
                kind="FILE",
                path=path,
                lines=text_lines,
                children=None,
                is_last_page=is_last_page,
                next_page_start=next_page_start,
                truncated=truncated,
                binary=False,
            )
        if lines is not None:
            raise ValueError(
                "Bitbucket returned an unexpected browse response shape for "
                f"'{path}': 'lines' is {type(lines).__name__}, not a list. "
                "Refusing to report the malformed body as file content."
            )
        if data:
            # A non-empty body matching no recognised shape is a parse failure
            # and raises.
            raise ValueError(
                "Bitbucket returned an unexpected browse response shape for "
                f"'{path}': neither 'children' nor 'lines' nor a 'binary' flag "
                "is present. Refusing to report the malformed body as an empty "
                "result."
            )
        # A completely empty body is accepted as an empty file.
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
