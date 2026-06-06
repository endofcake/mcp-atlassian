"""Bitbucket Data Center pull-request diff models.

Models the structured (JSON) diff Bitbucket Data Center returns for a pull
request — a nested ``RestDiff`` → ``RestDiffHunk`` → ``RestDiffSegment`` →
``RestDiffLine`` shape, **not** raw unified-diff text. A per-file line budget
(``max_lines_per_file``) truncates oversized files at model-build time so the
diff payload stays bounded for token control; truncation is surfaced explicitly
via ``line_truncated``/``omitted_lines`` and the top-level ``truncated`` flag,
distinct from the server's own ``truncated`` markers.
"""

import logging
from typing import Any

from ..base import ApiModel
from ..constants import EMPTY_STRING

logger = logging.getLogger(__name__)


def _path_from_ref(ref: Any) -> str | None:
    """Join a diff path object (``{components, name, parent, ...}``) to a string.

    Args:
        ref: A ``source``/``destination`` object from a ``RestDiff``, or None for
            an added (no source) or deleted (no destination) file.

    Returns:
        The slash-joined path (e.g. ``"path/to/file.txt"``), or None when the ref
        is absent.
    """
    if not isinstance(ref, dict):
        return None
    components = ref.get("components")
    if isinstance(components, list) and components:
        return "/".join(str(c) for c in components)
    name = ref.get("name")
    return str(name) if name else None


def _dict_lines(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a segment's ``lines`` as a list of dicts (filtering malformed)."""
    raw = segment.get("lines")
    if not isinstance(raw, list):
        return []
    return [line for line in raw if isinstance(line, dict)]


def _count_hunk_lines(hunk: dict[str, Any]) -> int:
    """Count the diff lines in a raw hunk (for omitted-line accounting)."""
    segments = hunk.get("segments")
    if not isinstance(segments, list):
        return 0
    return sum(len(_dict_lines(seg)) for seg in segments if isinstance(seg, dict))


class BitbucketDiffLine(ApiModel):
    """A single line within a diff segment (``RestDiffLine``)."""

    line: str = EMPTY_STRING
    source: int | None = None
    destination: int | None = None
    truncated: bool | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketDiffLine":
        """Create a BitbucketDiffLine from a Bitbucket API line object."""
        if not isinstance(data, dict) or not data:
            return cls()
        return cls(
            line=str(data.get("line") or EMPTY_STRING),
            source=data.get("source"),
            destination=data.get("destination"),
            truncated=data.get("truncated"),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {"line": self.line}
        if self.source is not None:
            result["source"] = self.source
        if self.destination is not None:
            result["destination"] = self.destination
        if self.truncated:
            result["truncated"] = self.truncated
        return result


class BitbucketDiffSegment(ApiModel):
    """A run of same-typed lines within a hunk (``RestDiffSegment``).

    ``type`` is one of ``ADDED``, ``CONTEXT``, or ``REMOVED``.
    """

    type: str | None = None
    lines: list[BitbucketDiffLine] = []
    truncated: bool | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketDiffSegment":
        """Create a BitbucketDiffSegment from a Bitbucket API segment object."""
        if not isinstance(data, dict) or not data:
            return cls()
        return cls(
            type=data.get("type"),
            lines=[
                BitbucketDiffLine.from_api_response(line) for line in _dict_lines(data)
            ],
            truncated=data.get("truncated"),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.type:
            result["type"] = self.type
        result["lines"] = [line.to_simplified_dict() for line in self.lines]
        if self.truncated:
            result["truncated"] = self.truncated
        return result


class BitbucketDiffHunk(ApiModel):
    """A contiguous region of a file diff (``RestDiffHunk``).

    Carries the ``@@`` header coordinates (source/destination line and span), the
    optional ``context`` string, and the ordered segments. ``truncated`` reflects
    the server's own hunk-level truncation.
    """

    context: str | None = None
    source_line: int | None = None
    source_span: int | None = None
    destination_line: int | None = None
    destination_span: int | None = None
    segments: list[BitbucketDiffSegment] = []
    truncated: bool | None = None

    @classmethod
    def _build(
        cls, data: dict[str, Any], remaining: int | None
    ) -> tuple["BitbucketDiffHunk", int, int, bool]:
        """Build a hunk, capping its lines at ``remaining`` (None = unlimited).

        Args:
            data: The raw ``RestDiffHunk`` object.
            remaining: The line budget left for the enclosing file, or None for no
                budget.

        Returns:
            A ``(hunk, used, omitted, truncated)`` tuple: the built hunk, how many
            lines it consumed from the budget, how many it dropped, and whether it
            dropped any.
        """
        segments: list[BitbucketDiffSegment] = []
        used = 0
        omitted = 0
        truncated = False
        budget = remaining
        raw_segments = data.get("segments")
        raw_segments = raw_segments if isinstance(raw_segments, list) else []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, dict):
                continue
            lines = _dict_lines(raw_segment)
            if budget is None:
                kept = lines
            elif budget <= 0:
                omitted += len(lines)
                truncated = True
                continue
            else:
                kept = lines[:budget]
                if len(lines) > budget:
                    omitted += len(lines) - budget
                    truncated = True
            used += len(kept)
            if budget is not None:
                budget -= len(kept)
            segments.append(
                BitbucketDiffSegment(
                    type=raw_segment.get("type"),
                    lines=[BitbucketDiffLine.from_api_response(line) for line in kept],
                    truncated=raw_segment.get("truncated"),
                )
            )
        hunk = cls(
            context=data.get("context"),
            source_line=data.get("sourceLine"),
            source_span=data.get("sourceSpan"),
            destination_line=data.get("destinationLine"),
            destination_span=data.get("destinationSpan"),
            segments=segments,
            truncated=data.get("truncated"),
        )
        return hunk, used, omitted, truncated

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketDiffHunk":
        """Create a BitbucketDiffHunk from a Bitbucket API hunk object."""
        hunk, _used, _omitted, _truncated = cls._build(data, None)
        return hunk

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.context:
            result["context"] = self.context
        if self.source_line is not None:
            result["source_line"] = self.source_line
        if self.source_span is not None:
            result["source_span"] = self.source_span
        if self.destination_line is not None:
            result["destination_line"] = self.destination_line
        if self.destination_span is not None:
            result["destination_span"] = self.destination_span
        result["segments"] = [seg.to_simplified_dict() for seg in self.segments]
        if self.truncated:
            result["truncated"] = self.truncated
        return result


class BitbucketFileDiff(ApiModel):
    """The diff of a single file within a pull request (``RestDiff``).

    ``source_path``/``destination_path`` are the file's before/after paths (one is
    None for an added or deleted file). ``server_truncated`` reflects the server's
    own per-file truncation; ``line_truncated`` and ``omitted_lines`` reflect this
    client's ``max_lines_per_file`` cap.
    """

    source_path: str | None = None
    destination_path: str | None = None
    binary: bool | None = None
    hunks: list[BitbucketDiffHunk] = []
    server_truncated: bool = False
    line_truncated: bool = False
    omitted_lines: int = 0

    @classmethod
    def from_api_response(
        cls,
        data: dict[str, Any],
        *,
        max_lines_per_file: int | None = None,
        **kwargs: Any,
    ) -> "BitbucketFileDiff":
        """Create a BitbucketFileDiff, capping lines at ``max_lines_per_file``.

        Args:
            data: The ``RestDiff`` object for one file.
            max_lines_per_file: Maximum diff lines to retain for this file; lines
                beyond the cap are dropped and counted in ``omitted_lines``. None
                disables the cap.

        Returns:
            A BitbucketFileDiff instance (a default instance for empty/non-dict
            input).
        """
        if not isinstance(data, dict) or not data:
            return cls()

        raw_hunks = data.get("hunks")
        raw_hunks = raw_hunks if isinstance(raw_hunks, list) else []
        hunks: list[BitbucketDiffHunk] = []
        remaining = max_lines_per_file
        omitted = 0
        line_truncated = False
        for raw_hunk in raw_hunks:
            if not isinstance(raw_hunk, dict):
                continue
            if remaining is not None and remaining <= 0:
                omitted += _count_hunk_lines(raw_hunk)
                line_truncated = True
                continue
            hunk, used, hunk_omitted, hunk_truncated = BitbucketDiffHunk._build(
                raw_hunk, remaining
            )
            if hunk.segments:
                hunks.append(hunk)
            if remaining is not None:
                remaining -= used
            omitted += hunk_omitted
            line_truncated = line_truncated or hunk_truncated

        return cls(
            source_path=_path_from_ref(data.get("source")),
            destination_path=_path_from_ref(data.get("destination")),
            binary=data.get("binary"),
            hunks=hunks,
            server_truncated=bool(data.get("truncated")),
            line_truncated=line_truncated,
            omitted_lines=omitted,
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.source_path:
            result["source_path"] = self.source_path
        if self.destination_path:
            result["destination_path"] = self.destination_path
        if self.binary is not None:
            result["binary"] = self.binary
        result["hunks"] = [hunk.to_simplified_dict() for hunk in self.hunks]
        if self.server_truncated:
            result["server_truncated"] = self.server_truncated
        if self.line_truncated:
            result["line_truncated"] = self.line_truncated
        if self.omitted_lines:
            result["omitted_lines"] = self.omitted_lines
        return result


class BitbucketPullRequestDiff(ApiModel):
    """The structured diff of a pull request (the ``/diff`` endpoint body).

    The endpoint returns a ``RestDiffResponse`` whose ``diffs`` array holds one
    ``RestDiff`` per changed file. ``total_files`` is the number of files the
    server reported; ``count`` is the number returned after the ``max_files`` cap.
    ``truncated`` is the authoritative completeness signal: it is true if the
    response-level ``truncated`` flag is set, the file list was capped, any file's
    lines were dropped, or the server truncated a file.
    """

    files: list[BitbucketFileDiff] = []
    total_files: int = 0
    truncated: bool = False

    @classmethod
    def from_api_response(
        cls,
        data: dict[str, Any],
        *,
        max_lines_per_file: int | None = None,
        max_files: int | None = None,
        **kwargs: Any,
    ) -> "BitbucketPullRequestDiff":
        """Create a BitbucketPullRequestDiff from the diff endpoint body.

        Args:
            data: The diff endpoint response body (a ``RestDiffResponse``:
                ``{fromHash, toHash, diffs: [RestDiff], truncated}``).
            max_lines_per_file: Per-file diff-line cap (see
                :meth:`BitbucketFileDiff.from_api_response`).
            max_files: Maximum number of file diffs to retain; excess files are
                dropped and signalled via ``truncated``. None disables the cap.

        Returns:
            A BitbucketPullRequestDiff instance (an empty diff for a non-dict body
            or a body whose ``diffs`` is absent/misshaped).
        """
        if not isinstance(data, dict):
            return cls()

        raw_files = data.get("diffs")
        if data and not isinstance(raw_files, list):
            logger.warning(
                "Unexpected diff response shape: 'diffs' is absent or not a list "
                "in a non-empty body; returning an empty diff."
            )
        raw_files = (
            [f for f in raw_files if isinstance(f, dict)]
            if isinstance(raw_files, list)
            else []
        )
        total_files = len(raw_files)
        capped = raw_files[:max_files] if max_files is not None else raw_files
        files = [
            BitbucketFileDiff.from_api_response(
                f, max_lines_per_file=max_lines_per_file
            )
            for f in capped
        ]
        files_truncated = max_files is not None and total_files > max_files
        truncated = (
            bool(data.get("truncated"))
            or files_truncated
            or any(f.line_truncated or f.server_truncated for f in files)
        )
        return cls(files=files, total_files=total_files, truncated=truncated)

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        return {
            "files": [f.to_simplified_dict() for f in self.files],
            "count": len(self.files),
            "total_files": self.total_files,
            "truncated": self.truncated,
        }
