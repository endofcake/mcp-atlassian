"""Bitbucket Data Center API module for mcp_atlassian.

Exposes ``BitbucketConfig`` for authenticating to a Bitbucket Data Center
instance with an OAuth 2.0 bearer token. The client and domain operations
build on this configuration, mirroring the Jira/Confluence architecture.
"""

from .config import BitbucketConfig

__all__ = [
    "BitbucketConfig",
]
