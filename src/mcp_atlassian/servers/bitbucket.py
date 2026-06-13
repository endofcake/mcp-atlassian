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
from mcp_atlassian.bitbucket.repositories import (
    DEFAULT_REPOS_LIMIT,
    MAX_REPOS_LIMIT,
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
            "projects": [project.to_simplified_dict() for project in page.projects],
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
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of pull requests to return. If more exist than "
                "are returned, the response sets 'truncated' to true."
            ),
            default=DEFAULT_PRS_LIMIT,
            ge=1,
            le=MAX_PRS_LIMIT,
        ),
    ] = DEFAULT_PRS_LIMIT,
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
        limit: Maximum number of pull requests to return.

    Returns:
        JSON string with the list of pull requests plus ``count``,
        ``is_last_page``, and ``truncated`` pagination flags, or a sanitised
        error object on failure.
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
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "pull_requests": [pr.to_simplified_dict() for pr in page.pull_requests],
            "count": len(page.pull_requests),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
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
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of activity entries to return. If more exist "
                "than are returned, the response sets 'truncated' to true."
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
        limit: Maximum number of activity entries to return.

    Returns:
        JSON string with the activity timeline plus ``count``, ``is_last_page``,
        and ``truncated`` pagination flags, or a sanitised error object on
        failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.get_activities(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            limit=limit,
        )
        response_data: dict[str, object] = {
            "success": True,
            "activities": [a.to_simplified_dict() for a in page.activities],
            "count": len(page.activities),
            "is_last_page": page.is_last_page,
            "truncated": page.truncated,
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
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of comments to return. If more exist than are "
                "returned, the response sets 'truncated' to true."
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
        limit: Maximum number of comments to return.

    Returns:
        JSON string with the comments plus ``count``, ``is_last_page``, and
        ``truncated`` pagination flags, or a sanitised error object on failure.
    """
    try:
        bitbucket = await get_bitbucket_fetcher(ctx)
        page = bitbucket.get_activities(
            project_key=project_key,
            repository_slug=repository_slug,
            pull_request_id=pull_request_id,
            action="COMMENTED",
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
