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
    BitbucketResourceNotFoundError,
)
from mcp_atlassian.bitbucket.commits import (
    DEFAULT_COMMITS_LIMIT,
    MAX_COMMITS_LIMIT,
)
from mcp_atlassian.bitbucket.pull_requests import (
    DEFAULT_ACTIVITIES_LIMIT,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_LINES_PER_FILE,
    DEFAULT_PRS_LIMIT,
    MAX_ACTIVITIES_LIMIT,
    MAX_MAX_FILES,
    MAX_MAX_LINES_PER_FILE,
    MAX_PRS_LIMIT,
)
from mcp_atlassian.bitbucket.refs import (
    DEFAULT_REFS_LIMIT,
    MAX_REFS_LIMIT,
)
from mcp_atlassian.bitbucket.repositories import (
    DEFAULT_REPOS_LIMIT,
    MAX_REPOS_LIMIT,
)
from mcp_atlassian.bitbucket.source import (
    DEFAULT_BROWSE_LIMIT,
    MAX_BROWSE_LIMIT,
)
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.servers.dependencies import get_bitbucket_fetcher
from mcp_atlassian.utils.decorators import check_write_access

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
    name: Annotated[
        str | None,
        Field(
            description=(
                "Optional server-side filter on project name. The match semantics "
                "(substring vs exact) depend on the instance, so if you get fewer "
                "results than expected, check 'is_last_page' and page further "
                "before concluding a project does not exist."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first project to return. "
                "Use 0 (default) for the first window, then pass the response's "
                "'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of projects to return in this window. Bitbucket "
                "Data Center paginates projects; if more exist than are returned, "
                "the response sets 'truncated' to true and 'next_page_start' to "
                "the cursor for the next call."
            ),
            default=DEFAULT_PROJECTS_LIMIT,
            ge=1,
            le=MAX_PROJECTS_LIMIT,
        ),
    ] = DEFAULT_PROJECTS_LIMIT,
    summary: Annotated[
        bool,
        Field(
            description=(
                "When true, return only each project's identity fields (key, "
                "name) instead of the full record — for scanning a large list to "
                "pick one before fetching its full detail."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List Bitbucket Data Center projects visible to the authenticated user.

    Args:
        ctx: The FastMCP context.
        name: Optional server-side filter on project name; the match semantics
            are decided by the instance.
        start: Pagination cursor (offset of the first project); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of projects to return in this window.

    Returns:
        JSON string with the list of projects plus ``count``, ``is_last_page``,
        ``truncated``, and ``next_page_start`` pagination fields, or a sanitised
        error object on failure. ``count`` is the number of projects in THIS
        window, not a grand total (Data Center does not report one). To get more,
        call again with ``start`` set to the returned ``next_page_start`` (when it
        is non-null); ``is_last_page=true`` means the list is complete. When a
        projects filter is configured the count is post-filter, so a window may be
        empty with ``is_last_page=false`` — keep paging with
        ``start=next_page_start`` until ``is_last_page`` is true.
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
        page = bitbucket.list_projects(name=name, start=start, limit=limit)
        response_data: dict[str, object] = {
            "success": True,
            "projects": [
                project.to_summary_dict() if summary else project.to_simplified_dict()
                for project in page.projects
            ],
            "count": len(page.projects),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_projects failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"list_projects failed: {e}")
        response_data = {
            "success": False,
            "error": f"Not Found: {str(e)}",
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
    name: Annotated[
        str | None,
        Field(
            description=(
                "Optional repository-name filter, matched case-insensitively "
                "(surrounding whitespace ignored). When set, results come from a "
                "cross-project search scoped to this project — a slightly wider "
                "visibility surface than the unfiltered project listing. Re-pass "
                "'name' on every page; dropping it on a resume switches endpoints "
                "and invalidates the prior 'next_page_start'."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first repository to "
                "return. Use 0 (default) for the first window, then pass the "
                "response's 'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of repositories to return in this window. "
                "Bitbucket Data Center paginates repositories; if more exist than "
                "are returned, the response sets 'truncated' to true and "
                "'next_page_start' to the cursor for the next call."
            ),
            default=DEFAULT_REPOS_LIMIT,
            ge=1,
            le=MAX_REPOS_LIMIT,
        ),
    ] = DEFAULT_REPOS_LIMIT,
    summary: Annotated[
        bool,
        Field(
            description=(
                "When true, return only each repository's identity fields (slug, "
                "name) instead of the full record — for scanning a large list to "
                "pick one before fetching its full detail."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List repositories in a Bitbucket Data Center project.

    Args:
        ctx: The FastMCP context.
        project_key: The project key whose repositories to list.
        name: Optional repository-name filter, matched case-insensitively
            (surrounding whitespace ignored). When set, results come from a
            cross-project search scoped to this project — a slightly wider
            visibility surface than the unfiltered listing.
        start: Pagination cursor (offset of the first repository); 0 for the
            first window, else a prior response's ``next_page_start``.
        limit: Maximum number of repositories to return in this window.

    Returns:
        JSON string with the list of repositories plus ``count``,
        ``is_last_page``, ``truncated``, and ``next_page_start`` pagination
        fields, or a sanitised error object on failure. ``count`` is the number
        of repositories in THIS window, not a grand total (Data Center does not
        report one). To get more, call again with ``start`` set to the returned
        ``next_page_start`` (when it is non-null); ``is_last_page=true`` means the
        list is complete.
    """
    # Mirrors list_projects' error handling: each branch logs once, server-side,
    # and returns only a sanitised message to the client. The fetcher signals a
    # blank project_key, connection failures, non-auth HTTP statuses, and
    # non-JSON responses as ValueError (with a crafted, leak-free message), and
    # token rejection as MCPAtlassianAuthenticationError.
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.list_repositories(
            project_key=project_key, name=name, start=start, limit=limit
        )
        response_data: dict[str, object] = {
            "success": True,
            "repositories": [
                repo.to_summary_dict() if summary else repo.to_simplified_dict()
                for repo in page.repositories
            ],
            "count": len(page.repositories),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_repositories failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"list_repositories failed: {e}")
        response_data = {
            "success": False,
            "error": f"Not Found: {str(e)}",
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


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "List Bitbucket Branches", "readOnlyHint": True},
)
async def list_branches(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description=(
                "The Bitbucket project key (e.g. 'PROJ'). Use list_projects to "
                "discover available keys."
            ),
        ),
    ],
    repository_slug: Annotated[
        str,
        Field(
            description=(
                "The repository slug (e.g. 'my-repo'). Use list_repositories to "
                "discover available slugs."
            ),
        ),
    ],
    filter_text: Annotated[
        str | None,
        Field(
            description=(
                "Optional server-side filter on branch name (substring match). "
                "If you get fewer results than expected, check 'is_last_page' and "
                "page further before concluding a branch does not exist."
            ),
            default=None,
        ),
    ] = None,
    order_by: Annotated[
        str | None,
        Field(
            description=(
                "Optional ordering: 'ALPHABETICAL' or 'MODIFICATION' (most "
                "recently modified first). An unrecognised value falls back to "
                "the server default."
            ),
            default=None,
        ),
    ] = None,
    boost_matches: Annotated[
        bool | None,
        Field(
            description=(
                "When true, floats exact and prefix matches of 'filter_text' to "
                "the top of the results. Pair with 'filter_text'."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first branch to return. "
                "Use 0 (default) for the first window, then pass the response's "
                "'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of branches to return in this window. Bitbucket "
                "Data Center paginates branches; if more exist than are returned, "
                "the response sets 'truncated' to true and 'next_page_start' to "
                "the cursor for the next call."
            ),
            default=DEFAULT_REFS_LIMIT,
            ge=1,
            le=MAX_REFS_LIMIT,
        ),
    ] = DEFAULT_REFS_LIMIT,
    summary: Annotated[
        bool,
        Field(
            description=(
                "When true, return only each branch's triage fields (display_id, "
                "latest_commit) instead of the full record — for scanning a large "
                "list to pick one."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List branches in a Bitbucket Data Center repository.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        filter_text: Optional server-side branch-name filter (substring match).
        order_by: Optional ordering — ALPHABETICAL or MODIFICATION.
        boost_matches: When true, floats exact/prefix filter matches up.
        start: Pagination cursor (offset of the first branch); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of branches to return in this window.

    Returns:
        JSON string with the list of branches plus ``count``, ``is_last_page``,
        ``truncated``, and ``next_page_start`` pagination fields, or a sanitised
        error object on failure. ``count`` is the number of branches in THIS
        window, not a grand total (Data Center does not report one). To get more,
        call again with ``start`` set to the returned ``next_page_start`` (when it
        is non-null); ``is_last_page=true`` means the list is complete.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.list_branches(
            project_key=project_key,
            repository_slug=repository_slug,
            filter_text=filter_text,
            order_by=order_by,
            boost_matches=boost_matches,
            start=start,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "branches": [
                branch.to_summary_dict() if summary else branch.to_simplified_dict()
                for branch in page.branches
            ],
            "count": len(page.branches),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_branches failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"list_branches failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"list_branches failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in list_branches:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while listing branches.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "List Bitbucket Tags", "readOnlyHint": True},
)
async def list_tags(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description=(
                "The Bitbucket project key (e.g. 'PROJ'). Use list_projects to "
                "discover available keys."
            ),
        ),
    ],
    repository_slug: Annotated[
        str,
        Field(
            description=(
                "The repository slug (e.g. 'my-repo'). Use list_repositories to "
                "discover available slugs."
            ),
        ),
    ],
    filter_text: Annotated[
        str | None,
        Field(
            description=(
                "Optional server-side filter on tag name (substring match). If "
                "you get fewer results than expected, check 'is_last_page' and "
                "page further before concluding a tag does not exist."
            ),
            default=None,
        ),
    ] = None,
    order_by: Annotated[
        str | None,
        Field(
            description=(
                "Optional ordering: 'ALPHABETICAL' or 'MODIFICATION' (most "
                "recently modified first). An unrecognised value falls back to "
                "the server default."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first tag to return. Use 0 "
                "(default) for the first window, then pass the response's "
                "'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of tags to return in this window. Bitbucket Data "
                "Center paginates tags; if more exist than are returned, the "
                "response sets 'truncated' to true and 'next_page_start' to the "
                "cursor for the next call."
            ),
            default=DEFAULT_REFS_LIMIT,
            ge=1,
            le=MAX_REFS_LIMIT,
        ),
    ] = DEFAULT_REFS_LIMIT,
    summary: Annotated[
        bool,
        Field(
            description=(
                "When true, return only each tag's triage fields (display_id, "
                "latest_commit) instead of the full record — for scanning a large "
                "list to pick one."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List tags in a Bitbucket Data Center repository.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        filter_text: Optional server-side tag-name filter (substring match).
        order_by: Optional ordering — ALPHABETICAL or MODIFICATION.
        start: Pagination cursor (offset of the first tag); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of tags to return in this window.

    Returns:
        JSON string with the list of tags plus ``count``, ``is_last_page``,
        ``truncated``, and ``next_page_start`` pagination fields, or a sanitised
        error object on failure. ``count`` is the number of tags in THIS window,
        not a grand total (Data Center does not report one). To get more, call
        again with ``start`` set to the returned ``next_page_start`` (when it is
        non-null); ``is_last_page=true`` means the list is complete.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.list_tags(
            project_key=project_key,
            repository_slug=repository_slug,
            filter_text=filter_text,
            order_by=order_by,
            start=start,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "tags": [
                tag.to_summary_dict() if summary else tag.to_simplified_dict()
                for tag in page.tags
            ],
            "count": len(page.tags),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_tags failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"list_tags failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"list_tags failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in list_tags:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while listing tags.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Get Bitbucket Tag", "readOnlyHint": True},
)
async def get_tag(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "The tag name (e.g. 'v1.0.0' or 'release/1.0'). Slashes and "
                "special characters are handled."
            ),
        ),
    ],
) -> str:
    """Get a single tag in a Bitbucket Data Center repository.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        name: The tag name.

    Returns:
        JSON string with the tag (display id, ref id, latest commit, and the
        annotated-tag ``hash`` when present), or a sanitised error object on
        failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        tag = bitbucket.get_tag(
            project_key=project_key,
            repository_slug=repository_slug,
            name=name,
        )
        response_data: dict[str, object] = {
            "success": True,
            "tag": tag.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_tag failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"get_tag failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_tag failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_tag:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while getting the tag.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Get Bitbucket Default Branch", "readOnlyHint": True},
)
async def get_default_branch(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
) -> str:
    """Get a Bitbucket Data Center repository's default branch.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.

    Returns:
        JSON string with the default branch as a minimal ref (display id, ref
        id, type — no commit SHA), or a sanitised error object on failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        branch = bitbucket.get_default_branch(
            project_key=project_key,
            repository_slug=repository_slug,
        )
        response_data: dict[str, object] = {
            "success": True,
            "branch": branch.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_default_branch failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"get_default_branch failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_default_branch failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_default_branch:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while getting the default branch.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "List Bitbucket Commits", "readOnlyHint": True},
)
async def list_commits(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description=(
                "The Bitbucket project key (e.g. 'PROJ'). Use list_projects to "
                "discover available keys."
            ),
        ),
    ],
    repository_slug: Annotated[
        str,
        Field(
            description=(
                "The repository slug (e.g. 'my-repo'). Use list_repositories to "
                "discover available slugs."
            ),
        ),
    ],
    since: Annotated[
        str | None,
        Field(
            description=(
                "Optional exclusive lower-bound commit SHA or ref to start the "
                "history from. Applied server-side."
            ),
            default=None,
        ),
    ] = None,
    until: Annotated[
        str | None,
        Field(
            description=(
                "Optional inclusive upper-bound commit SHA or ref (e.g. a branch "
                "name like 'main' or a tip SHA). Applied server-side."
            ),
            default=None,
        ),
    ] = None,
    path: Annotated[
        str | None,
        Field(
            description=(
                "Optional path to restrict the history to (file or directory). "
                "Applied server-side. Required when using 'follow_renames'."
            ),
            default=None,
        ),
    ] = None,
    merges: Annotated[
        str | None,
        Field(
            description=(
                "Optional merge-commit handling: 'exclude', 'include', or 'only'. "
                "An unrecognised value falls back to the server default."
            ),
            default=None,
        ),
    ] = None,
    follow_renames: Annotated[
        bool | None,
        Field(
            description=(
                "When true, follow a file's history across renames. Only valid "
                "together with a single-file 'path'."
            ),
            default=None,
        ),
    ] = None,
    ignore_missing: Annotated[
        bool | None,
        Field(
            description=(
                "When true, ignore missing commits instead of failing the call."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first commit to return. "
                "Use 0 (default) for the first window, then pass the response's "
                "'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of commits to return in this window. Bitbucket "
                "Data Center paginates commits; if more exist than are returned, "
                "the response sets 'truncated' to true and 'next_page_start' to "
                "the cursor for the next call."
            ),
            default=DEFAULT_COMMITS_LIMIT,
            ge=1,
            le=MAX_COMMITS_LIMIT,
        ),
    ] = DEFAULT_COMMITS_LIMIT,
    summary: Annotated[
        bool,
        Field(
            description=(
                "When true, return only each commit's triage fields (display_id, "
                "author, author_timestamp, first message line) instead of the "
                "full record — for scanning a large history to pick one."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List commits in a Bitbucket Data Center repository.

    The history filters ('since', 'until', 'path', 'merges', 'follow_renames')
    are applied server-side, not by walking the result. 'follow_renames' is only
    valid together with a single-file 'path'.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        since: Optional exclusive lower-bound commit/ref.
        until: Optional inclusive upper-bound commit/ref (e.g. a branch tip).
        path: Optional path to restrict the history to.
        merges: Optional merge-commit handling — exclude/include/only.
        follow_renames: When true, follow a file across renames (needs 'path').
        ignore_missing: When true, ignore missing commits rather than failing.
        start: Pagination cursor (offset of the first commit); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of commits to return in this window.

    Returns:
        JSON string with the list of commits plus ``count``, ``is_last_page``,
        ``truncated``, and ``next_page_start`` pagination fields, or a sanitised
        error object on failure. ``count`` is the number of commits in THIS
        window, not a grand total (Data Center does not report one). To get more,
        call again with ``start`` set to the returned ``next_page_start`` (when it
        is non-null); ``is_last_page=true`` means the list is complete.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.list_commits(
            project_key=project_key,
            repository_slug=repository_slug,
            since=since,
            until=until,
            path=path,
            merges=merges,
            follow_renames=follow_renames,
            ignore_missing=ignore_missing,
            start=start,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "commits": [
                commit.to_summary_dict() if summary else commit.to_simplified_dict()
                for commit in page.commits
            ],
            "count": len(page.commits),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_commits failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"list_commits failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"list_commits failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in list_commits:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while listing commits.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Get Bitbucket Commit", "readOnlyHint": True},
)
async def get_commit(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    commit_id: Annotated[
        str,
        Field(
            description=(
                "The commit SHA (full or abbreviated). Returned strictly by id."
            ),
        ),
    ],
) -> str:
    """Get a single commit by id in a Bitbucket Data Center repository.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        commit_id: The commit SHA (full or abbreviated).

    Returns:
        JSON string with the commit (id, display id, message, author/committer,
        timestamps, parents), or a sanitised error object on failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        commit = bitbucket.get_commit(
            project_key=project_key,
            repository_slug=repository_slug,
            commit_id=commit_id,
        )
        response_data: dict[str, object] = {
            "success": True,
            "commit": commit.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_commit failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"get_commit failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_commit failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_commit:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while getting the commit.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Browse Bitbucket Source", "readOnlyHint": True},
)
async def browse_path(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    path: Annotated[
        str,
        Field(
            description=(
                "The path to browse, relative to the repository root (e.g. "
                "'src/app.py' for a file or 'src' for a directory). Empty (the "
                "default) browses the repository root. Real path separators are "
                "preserved; '.'/'..' and traversal paths are rejected."
            ),
            default="",
        ),
    ] = "",
    at: Annotated[
        str | None,
        Field(
            description=(
                "Optional commit SHA, branch, or tag ref to read at. The "
                "repository's default branch is used when omitted."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the 0-based offset of the first line (file) "
                "or child (directory) to return. Use 0 (default) for the first "
                "window, then pass the response's 'next_page_start' to fetch the "
                "next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of lines (file) or children (directory) to "
                "return in this window. If more exist, the response sets "
                "'truncated' to true and 'next_page_start' to the next cursor."
            ),
            default=DEFAULT_BROWSE_LIMIT,
            ge=1,
            le=MAX_BROWSE_LIMIT,
        ),
    ] = DEFAULT_BROWSE_LIMIT,
) -> str:
    """Browse a file or directory in a Bitbucket Data Center repository.

    Returns either a directory listing OR a window of a file's text lines from
    one call — read the response's 'type' field ("FILE" or "DIRECTORY") to tell
    which. 'at' selects a commit/branch/tag (the default branch otherwise). Page
    large files or directories by calling again with 'start' set to the returned
    'next_page_start'. '.'/'..' and traversal paths are rejected. 'count' is the
    number of items in THIS window, not a grand total.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        path: The path to browse (empty for the repository root).
        at: Optional commit/branch/tag ref to read at.
        start: Pagination cursor (0-based line/child offset); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum lines (file) or children (directory) for this window.

    Returns:
        JSON string with ``type`` ("FILE"|"DIRECTORY"), ``path``, and either
        ``lines`` (file) or ``children`` (directory), plus ``binary`` (file
        only), ``count``, ``is_last_page``, ``truncated``, and
        ``next_page_start``; or a sanitised error object on failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        result = bitbucket.browse(
            project_key=project_key,
            repository_slug=repository_slug,
            path=path,
            at=at,
            start=start,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "type": result.kind,
            "path": result.path,
            "is_last_page": result.is_last_page,
            "truncated": result.truncated,
            "next_page_start": result.next_page_start,
        }
        if result.kind == "DIRECTORY":
            children = result.children or []
            response_data["children"] = [c.to_simplified_dict() for c in children]
            response_data["count"] = len(children)
        else:
            lines = result.lines or []
            response_data["lines"] = lines
            response_data["binary"] = result.binary
            response_data["count"] = len(lines)
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"browse_path failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"browse_path failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"browse_path failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in browse_path:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while browsing the path.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "List Bitbucket Pull Requests", "readOnlyHint": True},
)
async def list_pull_requests(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description=(
                "The Bitbucket project key (e.g. 'PROJ'). Use list_projects to "
                "discover available keys."
            ),
        ),
    ],
    repository_slug: Annotated[
        str,
        Field(
            description=(
                "The repository slug (e.g. 'my-repo'). Use list_repositories to "
                "discover available slugs."
            ),
        ),
    ],
    state: Annotated[
        str | None,
        Field(
            description=(
                "Filter by pull-request state: 'OPEN' (default), 'DECLINED', "
                "'MERGED', or 'ALL'."
            ),
            default=None,
        ),
    ] = None,
    direction: Annotated[
        str | None,
        Field(
            description=(
                "Direction relative to the repository: 'INCOMING' (default) or "
                "'OUTGOING'."
            ),
            default=None,
        ),
    ] = None,
    at: Annotated[
        str | None,
        Field(
            description=(
                "Optional fully-qualified branch ref to filter on (e.g. "
                "'refs/heads/main')."
            ),
            default=None,
        ),
    ] = None,
    order: Annotated[
        str | None,
        Field(
            description="Ordering: 'NEWEST' (default) or 'OLDEST'.",
            default=None,
        ),
    ] = None,
    filter_text: Annotated[
        str | None,
        Field(
            description="Optional substring match on PR title or description.",
            default=None,
        ),
    ] = None,
    draft: Annotated[
        bool | None,
        Field(
            description="Optional filter by draft status.",
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first pull request to "
                "return. Use 0 (default) for the first window, then pass the "
                "response's 'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of pull requests to return in this window. If "
                "more exist than are returned, the response sets 'truncated' to "
                "true and 'next_page_start' to the cursor for the next call."
            ),
            default=DEFAULT_PRS_LIMIT,
            ge=1,
            le=MAX_PRS_LIMIT,
        ),
    ] = DEFAULT_PRS_LIMIT,
    summary: Annotated[
        bool,
        Field(
            description=(
                "When true, return only each pull request's triage fields (id, "
                "title, state, author) instead of the full record — for scanning "
                "a large list to pick one before fetching its full detail."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List pull requests in a Bitbucket Data Center repository.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        state: Optional pull-request state filter.
        direction: Optional direction relative to the repository.
        at: Optional fully-qualified branch ref filter.
        order: Optional ordering.
        filter_text: Optional substring match on PR title or description.
        draft: Optional filter by draft status. The accepted wire values are
            confirmed at runtime.
        start: Pagination cursor (offset of the first pull request); 0 for the
            first window, else a prior response's ``next_page_start``.
        limit: Maximum number of pull requests to return in this window.

    Returns:
        JSON string with the list of pull requests plus ``count``,
        ``is_last_page``, ``truncated``, and ``next_page_start`` pagination
        fields, or a sanitised error object on failure. ``count`` is the number
        of pull requests in THIS window, not a grand total (Data Center does not
        report one). To get more, call again with ``start`` set to the returned
        ``next_page_start`` (when it is non-null); ``is_last_page=true`` means the
        list is complete.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.list_pull_requests(
            project_key=project_key,
            repository_slug=repository_slug,
            state=state,
            direction=direction,
            at=at,
            order=order,
            filter_text=filter_text,
            draft=draft,
            start=start,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "pull_requests": [
                pr.to_summary_dict() if summary else pr.to_simplified_dict()
                for pr in page.pull_requests
            ],
            "count": len(page.pull_requests),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"list_pull_requests failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"list_pull_requests failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"list_pull_requests failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in list_pull_requests:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while listing pull requests.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Get Bitbucket Pull Request", "readOnlyHint": True},
)
async def get_pull_request(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
) -> str:
    """Get a single Bitbucket Data Center pull request's metadata.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.

    Returns:
        JSON string with the pull request (metadata, refs, reviewer/approval
        state), or a sanitised error object on failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        pull_request = bitbucket.get_pull_request(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
        )
        response_data: dict[str, object] = {
            "success": True,
            "pull_request": pull_request.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_pull_request failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"get_pull_request failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_pull_request failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_pull_request:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while getting the pull request.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Get Bitbucket Pull Request Commits",
        "readOnlyHint": True,
    },
)
async def get_pull_request_commits(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first commit to return. "
                "Use 0 (default) for the first window, then pass the response's "
                "'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of commits to return in this window. Bitbucket "
                "Data Center paginates commits; if more exist than are returned, "
                "the response sets 'truncated' to true and 'next_page_start' to "
                "the cursor for the next call."
            ),
            default=DEFAULT_COMMITS_LIMIT,
            ge=1,
            le=MAX_COMMITS_LIMIT,
        ),
    ] = DEFAULT_COMMITS_LIMIT,
    summary: Annotated[
        bool,
        Field(
            description=(
                "When true, return only each commit's triage fields (display_id, "
                "author, author_timestamp, first message line) instead of the "
                "full record."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List the commits that make up a Bitbucket Data Center pull request.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        start: Pagination cursor (offset of the first commit); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of commits to return in this window.

    Returns:
        JSON string with the list of commits plus ``count``, ``is_last_page``,
        ``truncated``, and ``next_page_start`` pagination fields, or a sanitised
        error object on failure. ``count`` is the number of commits in THIS
        window, not a grand total. To get more, call again with ``start`` set to
        the returned ``next_page_start`` (when it is non-null);
        ``is_last_page=true`` means the list is complete.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.get_pull_request_commits(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            start=start,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "commits": [
                commit.to_summary_dict() if summary else commit.to_simplified_dict()
                for commit in page.commits
            ],
            "count": len(page.commits),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_pull_request_commits failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        # Must precede the ValueError branch (this subclasses ValueError).
        logger.error(f"get_pull_request_commits failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_pull_request_commits failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_pull_request_commits:")
        response_data = {
            "success": False,
            "error": (
                "An unexpected error occurred while getting the pull request commits."
            ),
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Get Bitbucket Pull Request Diff", "readOnlyHint": True},
)
async def get_pull_request_diff(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    max_lines_per_file: Annotated[
        int,
        Field(
            description=(
                "Maximum diff lines to return per file (token control). Files "
                "exceeding this are truncated; the response flags the file with "
                "'line_truncated' and counts the dropped lines in 'omitted_lines'."
            ),
            default=DEFAULT_MAX_LINES_PER_FILE,
            ge=1,
            le=MAX_MAX_LINES_PER_FILE,
        ),
    ] = DEFAULT_MAX_LINES_PER_FILE,
    max_files: Annotated[
        int,
        Field(
            description=(
                "Maximum number of changed files to return (token control). If "
                "the pull request changes more files than this, the response "
                "returns the first 'max_files' and sets 'truncated' to true; "
                "'total_files' reports the full count."
            ),
            default=DEFAULT_MAX_FILES,
            ge=1,
            le=MAX_MAX_FILES,
        ),
    ] = DEFAULT_MAX_FILES,
) -> str:
    """Get a Bitbucket Data Center pull request's structured diff.

    Returns the diff as a structured hunk model (files → hunks → segments →
    lines), not raw unified-diff text. The diff is bounded for token control:
    each file is capped at ``max_lines_per_file`` lines and the file list is
    capped at ``max_files``; the top-level ``truncated`` flag reports whether
    anything was omitted.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        max_lines_per_file: Per-file diff-line cap.
        max_files: Maximum number of changed files to return.

    Returns:
        JSON string with the structured diff (``files``, ``count``,
        ``total_files``, ``truncated``), or a sanitised error object on failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        diff = bitbucket.get_pull_request_diff(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            max_lines_per_file=max_lines_per_file,
            max_files=max_files,
        )
        response_data: dict[str, object] = {
            "success": True,
            "diff": diff.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_pull_request_diff failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"get_pull_request_diff failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_pull_request_diff failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_pull_request_diff:")
        response_data = {
            "success": False,
            "error": (
                "An unexpected error occurred while getting the pull request diff."
            ),
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Get Bitbucket Pull Request Activities",
        "readOnlyHint": True,
    },
)
async def get_pull_request_activities(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first activity entry to "
                "return. Use 0 (default) for the first window, then pass the "
                "response's 'next_page_start' to fetch the next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of activity entries to return in this window. If "
                "more exist than are returned, the response sets 'truncated' to "
                "true and 'next_page_start' to the cursor for the next call."
            ),
            default=DEFAULT_ACTIVITIES_LIMIT,
            ge=1,
            le=MAX_ACTIVITIES_LIMIT,
        ),
    ] = DEFAULT_ACTIVITIES_LIMIT,
) -> str:
    """Get a Bitbucket Data Center pull request's activity timeline.

    The timeline interleaves comments, approvals, merges, rescopes, and other
    actions in chronological order. For comments only, use
    get_pull_request_comments.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        start: Pagination cursor (offset of the first activity entry); 0 for the
            first window, else a prior response's ``next_page_start``.
        limit: Maximum number of activity entries to return in this window.

    Returns:
        JSON string with the activity timeline plus ``count``, ``is_last_page``,
        ``truncated``, and ``next_page_start`` pagination fields, or a sanitised
        error object on failure. ``count`` is the number of entries in THIS
        window, not a grand total (Data Center does not report one). To get more,
        call again with ``start`` set to the returned ``next_page_start`` (when it
        is non-null); ``is_last_page=true`` means the timeline is complete.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.get_activities(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            start=start,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "activities": [a.to_simplified_dict() for a in page.activities],
            "count": len(page.activities),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_pull_request_activities failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"get_pull_request_activities failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_pull_request_activities failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_pull_request_activities:")
        response_data = {
            "success": False,
            "error": (
                "An unexpected error occurred while getting pull request activities."
            ),
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Get Bitbucket Pull Request Comments",
        "readOnlyHint": True,
    },
)
async def get_pull_request_comments(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset into the activity timeline to "
                "resume the comment scan from. Use 0 (default) for the first "
                "window, then pass the response's 'next_page_start' to fetch the "
                "next window."
            ),
            default=0,
            ge=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of comments to return in this window. If more "
                "exist than are returned, the response sets 'truncated' to true "
                "and 'next_page_start' to the cursor for the next call."
            ),
            default=DEFAULT_ACTIVITIES_LIMIT,
            ge=1,
            le=MAX_ACTIVITIES_LIMIT,
        ),
    ] = DEFAULT_ACTIVITIES_LIMIT,
) -> str:
    """Get the comments on a Bitbucket Data Center pull request.

    A focused view over the activity timeline: only ``COMMENTED`` actions, with
    the comment payload (text, author, thread state) surfaced. Backed by the same
    activities endpoint as get_pull_request_activities.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        start: Pagination cursor (offset into the activity timeline); 0 for the
            first window, else a prior response's ``next_page_start``.
        limit: Maximum number of comments to return in this window.

    Returns:
        JSON string with the comments plus ``count``, ``is_last_page``,
        ``truncated``, and ``next_page_start`` pagination fields, or a sanitised
        error object on failure. ``count`` is the number of comments in THIS
        window, not a grand total (Data Center does not report one). Because the
        comment filter runs over the activity timeline, a window may be empty
        with ``is_last_page=false`` — keep paging with ``start=next_page_start``
        until ``is_last_page`` is true; that means the comments are complete.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.get_activities(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            action="COMMENTED",
            start=start,
            limit=limit,
        )
        comments = [
            a.comment.to_simplified_dict()
            for a in page.activities
            if a.comment is not None
        ]
        response_data: dict[str, object] = {
            "success": True,
            "comments": comments,
            "count": len(comments),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
            "next_page_start": page.next_page_start,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"get_pull_request_comments failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"get_pull_request_comments failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"get_pull_request_comments failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in get_pull_request_comments:")
        response_data = {
            "success": False,
            "error": (
                "An unexpected error occurred while getting pull request comments."
            ),
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Add Bitbucket Pull Request Comment",
        "readOnlyHint": False,
    },
)
@check_write_access
async def add_comment(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    text: Annotated[
        str,
        Field(description="The comment text (Markdown). Must be non-blank."),
    ],
    parent_id: Annotated[
        int | None,
        Field(
            description=(
                "Reply to this comment id. A reply inherits the parent thread's "
                "anchor, so it cannot be combined with any file/line parameter."
            ),
            default=None,
            ge=1,
        ),
    ] = None,
    file_path: Annotated[
        str | None,
        Field(
            description=(
                "Anchor the comment to this file path (a whole-file comment, or "
                "a line comment when 'line' is also given). Omit for a general "
                "pull-request comment."
            ),
            default=None,
        ),
    ] = None,
    line: Annotated[
        int | None,
        Field(
            description=(
                "Anchor to this 1-based line in the diff. Requires 'file_path' "
                "and 'line_type'. The comment lands on the PR's current "
                "effective diff (the same diff get_pull_request_diff returns)."
            ),
            default=None,
            ge=1,
        ),
    ] = None,
    line_type: Annotated[
        str | None,
        Field(
            description=(
                "Diff line kind for a line comment: 'ADDED', 'REMOVED', or "
                "'CONTEXT' (an unchanged line near the change). Required with "
                "'line'."
            ),
            default=None,
        ),
    ] = None,
    file_type: Annotated[
        str | None,
        Field(
            description=(
                "Diff side a line comment attaches to: 'FROM' (source) or 'TO' "
                "(destination). Defaults from 'line_type' (REMOVED→FROM, "
                "ADDED/CONTEXT→TO) when omitted."
            ),
            default=None,
        ),
    ] = None,
    severity: Annotated[
        str,
        Field(
            description=(
                "'NORMAL' (default) for a comment, or 'BLOCKER' to raise a task "
                "(a must-resolve item) — valid in any comment mode."
            ),
            default="NORMAL",
        ),
    ] = "NORMAL",
) -> str:
    """Add a comment to a Bitbucket Data Center pull request.

    The comment mode follows which optional parameters are supplied: a general
    comment (text only), a reply ('parent_id'), a whole-file comment
    ('file_path'), a line comment ('file_path' + 'line' + 'line_type'), or a
    BLOCKER task ('severity'). Line/file anchors land on the pull request's
    current effective diff. On Data Center this write needs only REPO_READ
    permission. Blocked when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        text: The comment text (Markdown); must be non-blank.
        parent_id: When set, reply to that comment id.
        file_path: When set, anchor the comment to this file.
        line: When set (with file_path), anchor to this 1-based diff line.
        line_type: ADDED/REMOVED/CONTEXT (required with line).
        file_type: FROM/TO; derived from line_type when omitted.
        severity: NORMAL (default) or BLOCKER (a must-resolve task).

    Returns:
        JSON string with the created comment (its 'id' and 'version', plus
        author/text/thread state), or a sanitised error object on failure.

    Raises:
        ValueError: If in read-only mode (surfaced as a ToolError).
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        comment = bitbucket.add_comment(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            text=text,
            parent_id=parent_id,
            file_path=file_path,
            line=line,
            line_type=line_type,
            file_type=file_type,
            severity=severity,
        )
        response_data: dict[str, object] = {
            "success": True,
            "comment": comment.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"add_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"add_comment failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"add_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in add_comment:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while adding the comment.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Set Bitbucket Pull Request Review Status",
        "readOnlyHint": False,
    },
)
@check_write_access
async def set_review_status(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    status: Annotated[
        str,
        Field(
            description=(
                "The review status to set as the authenticated user: "
                "'APPROVED', 'NEEDS_WORK' (the UI's 'Request changes'), or "
                "'UNAPPROVED' (withdraw a prior approval)."
            ),
        ),
    ],
) -> str:
    """Set the authenticated user's review status on a pull request.

    Sets the caller's own participant status — 'APPROVED', 'NEEDS_WORK'
    (request changes), or 'UNAPPROVED' (withdraw approval). On Data Center this
    needs only REPO_READ. Bitbucket forbids the pull-request **author** from
    setting a status; that attempt returns a clear error. Blocked when the
    server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        status: APPROVED, NEEDS_WORK, or UNAPPROVED.

    Returns:
        JSON string with the confirmed review status, or a sanitised error
        object on failure.

    Raises:
        ValueError: If in read-only mode (surfaced as a ToolError).
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        participant = bitbucket.set_review_status(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            status=status,
        )
        response_data: dict[str, object] = {
            "success": True,
            "participant": participant,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"set_review_status failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"set_review_status failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"set_review_status failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in set_review_status:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while setting the review status.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Edit Bitbucket Pull Request Comment",
        "readOnlyHint": False,
    },
)
@check_write_access
async def edit_comment(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    comment_id: Annotated[
        int,
        Field(description="The id of the comment to edit.", ge=1),
    ],
    text: Annotated[
        str,
        Field(description="The new comment text (Markdown). Must be non-blank."),
    ],
    version: Annotated[
        int,
        Field(
            description=(
                "The comment's current version, from add_comment or "
                "get_pull_request_comments. A 409 means the comment changed "
                "since you read it — re-fetch its version and retry."
            ),
            ge=0,
        ),
    ],
) -> str:
    """Edit the text of a Bitbucket Data Center pull-request comment.

    Replaces the comment text via an optimistic-locked update: 'version' must be
    the comment's current version (from add_comment or
    get_pull_request_comments). A 409 means the comment changed since you read
    it — re-fetch the version and retry. Only the comment **author** may edit
    its text: a non-author attempt returns 401 regardless of permission level.
    Blocked when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        comment_id: The id of the comment to edit.
        text: The new comment text (Markdown); must be non-blank.
        version: The comment's current version (optimistic-lock token).

    Returns:
        JSON string with the updated comment (its bumped 'version' and text), or
        a sanitised error object on failure.

    Raises:
        ValueError: If in read-only mode (surfaced as a ToolError).
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        comment = bitbucket.update_comment(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            comment_id=comment_id,
            version=version,
            text=text,
        )
        response_data: dict[str, object] = {
            "success": True,
            "comment": comment.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"edit_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"edit_comment failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"edit_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in edit_comment:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while editing the comment.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Resolve Bitbucket Pull Request Comment Thread",
        "readOnlyHint": False,
    },
)
@check_write_access
async def resolve_comment(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    comment_id: Annotated[
        int,
        Field(description="The id of the comment whose thread to resolve.", ge=1),
    ],
    version: Annotated[
        int,
        Field(
            description=(
                "The comment's current version, from add_comment or "
                "get_pull_request_comments. A 409 means the comment changed "
                "since you read it — re-fetch its version and retry."
            ),
            ge=0,
        ),
    ],
    resolved: Annotated[
        bool,
        Field(
            description=(
                "True (default) marks the comment thread resolved; False reopens it."
            ),
            default=True,
        ),
    ] = True,
) -> str:
    """Resolve or reopen a Bitbucket Data Center pull-request comment thread.

    Toggles the comment's thread-resolved state via an optimistic-locked update:
    'version' must be the comment's current version (from add_comment or
    get_pull_request_comments). A 409 means the comment changed since you read
    it — re-fetch the version and retry. This is thread resolution (the common
    review action), not task resolution. Blocked when the server runs with
    READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        comment_id: The id of the comment whose thread to resolve.
        version: The comment's current version (optimistic-lock token).
        resolved: True (default) resolves the thread; False reopens it.

    Returns:
        JSON string with the updated comment (its bumped 'version' and
        'thread_resolved' state), or a sanitised error object on failure.

    Raises:
        ValueError: If in read-only mode (surfaced as a ToolError).
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        comment = bitbucket.update_comment(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            comment_id=comment_id,
            version=version,
            thread_resolved=resolved,
        )
        response_data: dict[str, object] = {
            "success": True,
            "comment": comment.to_simplified_dict(),
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"resolve_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"resolve_comment failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"resolve_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in resolve_comment:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while resolving the comment.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Delete Bitbucket Pull Request Comment",
        "readOnlyHint": False,
    },
)
@check_write_access
async def delete_comment(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    pull_request_id: Annotated[
        int,
        Field(description="The pull-request id (a positive integer).", ge=1),
    ],
    comment_id: Annotated[
        int,
        Field(description="The id of the comment to delete.", ge=1),
    ],
    version: Annotated[
        int,
        Field(
            description=(
                "The comment's current version, from add_comment or "
                "get_pull_request_comments. A 409 may mean the version is stale "
                "OR the comment has replies (delete does not cascade)."
            ),
            ge=0,
        ),
    ],
) -> str:
    """Delete a Bitbucket Data Center pull-request comment.

    Deletes the comment via an optimistic-locked delete: 'version' must be the
    comment's current version (from add_comment or get_pull_request_comments).
    Delete does **not** cascade — a 409 may mean the version is stale, the
    comment has replies, or the repository is archived. Deleting another user's
    comment may require elevated (repo-admin) permission and otherwise returns
    401/409. Blocked when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        comment_id: The id of the comment to delete.
        version: The comment's current version (optimistic-lock token).

    Returns:
        JSON string confirming the delete (the deleted 'comment_id'), or a
        sanitised error object on failure.

    Raises:
        ValueError: If in read-only mode (surfaced as a ToolError).
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        bitbucket.delete_comment(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            comment_id=comment_id,
            version=version,
        )
        # A 204 has no body to echo, so confirm with the deleted id.
        response_data: dict[str, object] = {
            "success": True,
            "comment_id": comment_id,
        }
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"delete_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Authentication/Permission Error: {str(e)}",
        }
    except BitbucketResourceNotFoundError as e:
        logger.error(f"delete_comment failed: {e}")
        response_data = {"success": False, "error": f"Not Found: {str(e)}"}
    except (ValueError, OSError, HTTPError) as e:
        logger.error(f"delete_comment failed: {e}")
        response_data = {
            "success": False,
            "error": f"Network or API Error: {str(e)}",
        }
    except Exception:
        logger.exception("Unexpected error in delete_comment:")
        response_data = {
            "success": False,
            "error": "An unexpected error occurred while deleting the comment.",
        }
    return json.dumps(response_data, indent=2, ensure_ascii=False)
