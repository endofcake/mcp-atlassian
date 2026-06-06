"""Bitbucket Data Center FastMCP server instance and tool definitions.

Exposes read-only tools for querying a Bitbucket Data Center instance over its
OAuth 2.0-authenticated REST API.
"""

import json
import logging
from typing import Annotated

from fastmcp import Context, FastMCP
from pydantic import Field
from requests.exceptions import HTTPError

from mcp_atlassian.bitbucket.client import (
    DEFAULT_PROJECTS_LIMIT,
    MAX_PROJECTS_LIMIT,
)
from mcp_atlassian.bitbucket.repositories import (
    DEFAULT_REPOS_LIMIT,
    MAX_REPOS_LIMIT,
)
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.servers.dependencies import get_bitbucket_fetcher

logger = logging.getLogger(__name__)

bitbucket_mcp = FastMCP(
    name="Bitbucket MCP Service",
    instructions="Provides tools for interacting with Atlassian Bitbucket Data Center.",
)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_projects"},
    annotations={"title": "List Bitbucket Projects", "readOnlyHint": True},
)
async def list_projects(
    ctx: Context,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of projects to return. Bitbucket Data Center "
                "paginates projects; if more exist than are returned, the "
                "response sets 'truncated' to true."
            ),
            default=DEFAULT_PROJECTS_LIMIT,
            ge=1,
            le=MAX_PROJECTS_LIMIT,
        ),
    ] = DEFAULT_PROJECTS_LIMIT,
) -> str:
    """List Bitbucket Data Center projects visible to the authenticated user.

    Args:
        ctx: The FastMCP context.
        limit: Maximum number of projects to return.

    Returns:
        JSON string with the list of projects plus ``count``, ``is_last_page``,
        and ``truncated`` pagination flags, or a sanitised error object on
        failure. ``truncated`` is the authoritative completeness signal: when it
        is true, more projects exist than were returned — raise ``limit`` (if
        ``is_last_page`` is true) or narrow the query, and do not treat the list
        as exhaustive. When a projects filter is configured, a ``truncated``
        empty result means the scan did not reach the matching project, not that
        none exists.
    """
    # Each branch logs once, server-side, and returns only a sanitised message
    # to the client to avoid leaking internal details. Expected auth and
    # network/API errors log without a traceback; the unexpected branch logs
    # the full traceback for debugging.
    #
    # The fetcher signals connection failures, non-auth HTTP statuses, and
    # non-JSON responses as ValueError (with a crafted, leak-free message), and
    # token rejection as MCPAtlassianAuthenticationError. OSError/HTTPError are
    # included defensively in case a raw transport error ever surfaces.
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.list_projects(limit=limit)
        response_data: dict[str, object] = {
            "success": True,
            "projects": page.projects,
            "count": len(page.projects),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_projects failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"list_projects failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in list_projects:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while listing projects.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "List Bitbucket Repositories", "readOnlyHint": True},
)
async def list_repositories(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description=(
                "The Bitbucket project key whose repositories to list "
                "(e.g. 'PROJ'). Use list_projects to discover available keys."
            ),
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of repositories to return. Bitbucket Data Center "
                "paginates repositories; if more exist than are returned, the "
                "response sets 'truncated' to true."
            ),
            default=DEFAULT_REPOS_LIMIT,
            ge=1,
            le=MAX_REPOS_LIMIT,
        ),
    ] = DEFAULT_REPOS_LIMIT,
) -> str:
    """List repositories in a Bitbucket Data Center project.

    Args:
        ctx: The FastMCP context.
        project_key: The project key whose repositories to list.
        limit: Maximum number of repositories to return.

    Returns:
        JSON string with the list of repositories plus ``count``,
        ``is_last_page``, and ``truncated`` pagination flags, or a sanitised
        error object on failure. ``truncated`` is the authoritative completeness
        signal: when it is true, more repositories exist than were returned —
        raise ``limit`` (if ``is_last_page`` is true) and do not treat the list
        as exhaustive.
    """
    # Mirrors list_projects' error handling: each branch logs once, server-side,
    # and returns only a sanitised message to the client. The fetcher signals a
    # blank project_key, connection failures, non-auth HTTP statuses, and
    # non-JSON responses as ValueError (with a crafted, leak-free message), and
    # token rejection as MCPAtlassianAuthenticationError.
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.list_repositories(project_key=project_key, limit=limit)
        response_data: dict[str, object] = {
            "success": True,
            "repositories": [repo.to_simplified_dict() for repo in page.repositories],
            "count": len(page.repositories),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_repositories failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"list_repositories failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in list_repositories:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while listing repositories.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)
