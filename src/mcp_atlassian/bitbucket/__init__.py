"""Bitbucket Data Center API module for mcp_atlassian.

Exposes ``BitbucketConfig`` and ``BitbucketFetcher`` for authenticating to a
Bitbucket Data Center instance with an OAuth 2.0 bearer token. ``BitbucketFetcher``
composes single-responsibility domain mixins over the ``BitbucketClient`` base,
mirroring the Jira/Confluence architecture.
"""

from .builds import BuildsMixin
from .commits import CommitsMixin
from .compare import CompareMixin
from .config import BitbucketConfig
from .projects import ProjectsMixin
from .pull_requests import PullRequestsMixin
from .refs import RefsMixin
from .repositories import ReposMixin
from .source import SourceMixin
from .users import UsersMixin


class BitbucketFetcher(
    ProjectsMixin,
    ReposMixin,
    RefsMixin,
    PullRequestsMixin,
    CommitsMixin,
    SourceMixin,
    BuildsMixin,
    UsersMixin,
    CompareMixin,
):
    """Bitbucket Data Center client composing all domain mixins.

    Inherits the session, ``_get`` error taxonomy, and shared pagination helper
    from :class:`~mcp_atlassian.bitbucket.client.BitbucketClient`, and the
    domain operations from each mixin:

    - :class:`~mcp_atlassian.bitbucket.projects.ProjectsMixin`: project listing.
    - :class:`~mcp_atlassian.bitbucket.repositories.ReposMixin`: repository
      listing.
    - :class:`~mcp_atlassian.bitbucket.refs.RefsMixin`: branch/tag listing, tag
      lookup, and default-branch resolution.
    - :class:`~mcp_atlassian.bitbucket.pull_requests.PullRequestsMixin`:
      pull-request listing, metadata, structured diff, and activity timeline.
    - :class:`~mcp_atlassian.bitbucket.commits.CommitsMixin`: commit history,
      single-commit lookup, and pull-request commit listing.
    - :class:`~mcp_atlassian.bitbucket.source.SourceMixin`: directory and file
      browsing over the ``browse`` endpoint.
    - :class:`~mcp_atlassian.bitbucket.builds.BuildsMixin`: CI build statuses
      for a commit, over the ``build-status`` REST module.
    - :class:`~mcp_atlassian.bitbucket.users.UsersMixin`: the authenticated
      caller's profile.
    - :class:`~mcp_atlassian.bitbucket.compare.CompareMixin`: changed files,
      commits, and diff between two refs or commits.
    """

    pass


__all__ = [
    "BitbucketFetcher",
    "BitbucketConfig",
]
