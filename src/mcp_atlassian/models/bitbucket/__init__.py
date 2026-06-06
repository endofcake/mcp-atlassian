"""Bitbucket Data Center data models for the MCP Atlassian integration.

Pydantic models for Bitbucket Data Center API data structures, organized by
entity type, mirroring the Jira and Confluence model packages.
"""

from .project import BitbucketProject
from .repository import BitbucketRepository

__all__ = [
    "BitbucketProject",
    "BitbucketRepository",
]
