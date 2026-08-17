"""Bitbucket Data Center pull-request changed-file models.

Models one entry of the paged ``changes`` listing of a pull request (a
``RestChange``): the file's path, its previous path for a move or copy, the
change type, and the node type. The listing is the discovery step before a
per-file diff, so the projection keeps the fields a caller needs to pick a
file and drops the blob ids.
"""

from typing import Any

from ..base import ApiModel
from ._fields import _opt_bool, _opt_int, _opt_str
from ._paths import _path_from_ref


class BitbucketChange(ApiModel):
    """A single changed file of a pull request (a ``RestChange``).

    Attributes:
        path: The file's slash-joined path after the change.
        src_path: The file's previous path for a move, copy, or rename, or
            None.
        type: The change type: ``ADD``, ``COPY``, ``DELETE``, ``MODIFY``,
            ``MOVE``, or ``UNKNOWN``.
        node_type: ``FILE``, ``DIRECTORY``, or ``SUBMODULE``.
        executable: Whether the file is executable after the change.
        percent_unchanged: The similarity the server reports for a move or
            copy, or None.
        conflict: Whether the server reports a merge conflict on this file.
    """

    path: str | None = None
    src_path: str | None = None
    type: str | None = None
    node_type: str | None = None
    executable: bool = False
    percent_unchanged: int | None = None
    conflict: bool = False

    @classmethod
    def from_api_response(
        cls, data: dict[str, Any], **kwargs: Any
    ) -> "BitbucketChange":
        """Create a BitbucketChange from a ``changes`` entry.

        Args:
            data: One entry of the ``values`` list of the changes page.

        Returns:
            A BitbucketChange instance (a default instance for empty or
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
            src_path=_path_from_ref(data.get("srcPath")),
            type=_opt_str(data, "type", model=model),
            node_type=_opt_str(data, "nodeType", model=model),
            executable=_opt_bool(data, "executable", model=model) is True,
            percent_unchanged=_opt_int(data, "percentUnchanged", model=model),
            conflict=isinstance(data.get("conflict"), dict),
        )

    def to_simplified_dict(self) -> dict[str, Any]:
        """Convert to a simplified dictionary for API responses."""
        result: dict[str, Any] = {}
        if self.path:
            result["path"] = self.path
        if self.src_path:
            result["src_path"] = self.src_path
        if self.type:
            result["type"] = self.type
        if self.node_type:
            result["node_type"] = self.node_type
        if self.executable:
            result["executable"] = True
        if self.percent_unchanged is not None:
            result["percent_unchanged"] = self.percent_unchanged
        if self.conflict:
            result["conflict"] = True
        return result
