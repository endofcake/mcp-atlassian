"""Bitbucket Data Center API module for mcp_atlassian.

Exposes ``BitbucketConfig`` and ``BitbucketFetcher`` for authenticating to a
Bitbucket Data Center instance with an OAuth 2.0 bearer token. ``BitbucketFetcher``
composes single-responsibility domain mixins over the ``BitbucketClient`` base,
mirroring the Jira/Confluence architecture.
"""

from .config import BitbucketConfig
from .projects import ProjectsMixin
from .repositories import ReposMixin


class BitbucketFetcher(ProjectsMixin, ReposMixin):
    """Bitbucket Data Center client composing all domain mixins.

    Inherits the session, ``_get`` error taxonomy, and shared pagination helper
    from :class:`~mcp_atlassian.bitbucket.client.BitbucketClient`, and the
    domain operations from each mixin:

    - :class:`~mcp_atlassian.bitbucket.projects.ProjectsMixin`: project listing.
    - :class:`~mcp_atlassian.bitbucket.repositories.ReposMixin`: repository
      listing.
    """

    pass


__all__ = [
    "BitbucketFetcher",
    "BitbucketConfig",
]
