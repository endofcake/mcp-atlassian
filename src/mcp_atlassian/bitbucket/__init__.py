"""Bitbucket Data Center API module for mcp_atlassian.

Exposes ``BitbucketConfig`` and ``BitbucketFetcher`` for authenticating to a
Bitbucket Data Center instance with an OAuth 2.0 bearer token.
"""

from .client import BitbucketFetcher
from .config import BitbucketConfig

__all__ = [
    "BitbucketFetcher",
    "BitbucketConfig",
]
