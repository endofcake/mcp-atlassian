"""Shared path helpers for Bitbucket Data Center models.

Bitbucket DC returns the same ``{components, name, parent}`` path object in
several shapes — a ``RestDiff`` ``source``/``destination`` and a ``browse``
child entry's ``path`` — so the join logic lives here once and both reuse it.
"""

from typing import Any


def _path_from_ref(ref: Any) -> str | None:
    """Join a Bitbucket path object (``{components, name, parent, ...}``).

    Args:
        ref: A path object from a ``RestDiff`` ``source``/``destination`` or a
            ``browse`` child entry, or None when the ref is absent (e.g. an
            added file has no source, a deleted file no destination).

    Returns:
        The slash-joined path (e.g. ``"path/to/file.txt"``), or None when the
        ref is absent or carries no path information.
    """
    if not isinstance(ref, dict):
        return None
    components = ref.get("components")
    if isinstance(components, list) and components:
        return "/".join(str(c) for c in components)
    name = ref.get("name")
    return str(name) if name else None
