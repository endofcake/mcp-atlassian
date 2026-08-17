"""Bitbucket Data Center source-browse models.

Models a single child entry of a directory listing returned by the ``browse``
endpoint. A directory body carries ``children.values``, one entry per child,
each with a ``{components, name, parent}`` path object (the same shape the diff
models join), a ``type`` discriminator (FILE/DIRECTORY/SUBMODULE), a ``size``,
and a ``contentId`` blob hash.
"""

from typing import Any

from ..base import ApiModel
from ._fields import _opt_int, _opt_str
from ._paths import _path_from_ref


class BitbucketDirectoryEntry(ApiModel):
    """A single child of a directory listing (a ``browse`` child entry).

    Attributes:
        path: The child's slash-joined path (e.g. ``"src/app.py"``), built from
            the ``path`` object's ``components``.
        type: The entry kind (``FILE``, ``DIRECTORY``, or ``SUBMODULE``).
        size: The file size in bytes, or None for a directory/submodule.
        content_id: The blob hash (``contentId``) for a file, or None.
    """

    path: str | None = None
    type: str | None = None
    size: int | None = None
    content_id: str | None = None

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketDirectoryEntry":
        """Create a BitbucketDirectoryEntry from a browse child object.

        Args:
            data: A child entry from ``children.values`` of a browse directory
                body.

        Returns:
            A BitbucketDirectoryEntry instance (a default instance for empty or
            non-dict input).

        Raises:
            ValueError: If a scalar field is present with a value of the wrong
                type (see :mod:`mcp_atlassian.models.bitbucket._fields`).
        """
        if not isinstance(data, dict) or not data:
            return cls()
        model = cls.__name__
        return cls(
            path=_path_from_ref(data.get("path")),
            type=_opt_str(data, "type", model=model),
            size=_opt_int(data, "size", model=model),
            content_id=_opt_str(data, "contentId", model=model),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.path:
            result["path"] = self.path
        if self.type:
            result["type"] = self.type
        if self.size is not None:
            result["size"] = self.size
        if self.content_id:
            result["content_id"] = self.content_id
        return result
