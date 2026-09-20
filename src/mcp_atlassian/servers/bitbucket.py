"""Bitbucket Data Center FastMCP server instance and tool definitions.

Exposes read and write tools for working with a Bitbucket Data Center
instance over its OAuth 2.0-authenticated REST API.
"""

import json
from typing import Annotated

from fastmcp import Context
from pydantic import Field

from mcp_atlassian.bitbucket.builds import (
    DEFAULT_BUILD_STATUSES_LIMIT,
    MAX_BUILD_STATUSES_LIMIT,
)
from mcp_atlassian.bitbucket.client import (
    DEFAULT_PROJECTS_LIMIT,
    MAX_PROJECTS_LIMIT,
)
from mcp_atlassian.bitbucket.commits import (
    DEFAULT_COMMITS_LIMIT,
    MAX_COMMITS_LIMIT,
)
from mcp_atlassian.bitbucket.pull_requests import (
    DEFAULT_ACTIVITIES_LIMIT,
    DEFAULT_CHANGES_LIMIT,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_LINES_PER_FILE,
    DEFAULT_PRS_LIMIT,
    MAX_ACTIVITIES_LIMIT,
    MAX_CHANGES_LIMIT,
    MAX_CONTEXT_LINES,
    MAX_MAX_FILES,
    MAX_MAX_LINES_PER_FILE,
    MAX_PR_DESCRIPTION_CHARS,
    MAX_PR_TITLE_CHARS,
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
from mcp_atlassian.servers.async_utils import run_bitbucket_fetcher_call
from mcp_atlassian.servers.dependencies import get_bitbucket_fetcher
from mcp_atlassian.servers.error_handling import ErrorPreservingFastMCP
from mcp_atlassian.utils.decorators import check_write_access

bitbucket_mcp = ErrorPreservingFastMCP(
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
                "Maximum number of projects fetched for this window. A "
                "BITBUCKET_PROJECTS_FILTER allowlist configured on this MCP "
                "server applies to the fetched window, so fewer projects can be "
                "returned. If more exist than are "
                "returned, the response sets 'truncated' to true and "
                "'next_page_start' to the cursor for the next call."
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
                "name) instead of the full record, for scanning a large list to "
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
        limit: Maximum number of projects fetched for this window; a
            BITBUCKET_PROJECTS_FILTER allowlist applies to that window.
        summary: When true, project records carry identity fields only.

    Returns:
        JSON string with the list of projects plus ``count`` (the size of this window),
        ``is_last_page``, ``truncated``, and ``next_page_start``; the list is complete
        when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.

        An empty window with ``is_last_page`` false can still precede matches.
        Each call issues one upstream request and a configured projects filter
        narrows that window, so a window can hold zero projects while more
        pages remain. Keep paging with ``start=next_page_start`` until
        ``is_last_page`` is true before concluding a project is absent.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.list_projects, name=name, start=start, limit=limit
    )
    response_data: dict[str, object] = {
        "projects": [
            project.to_summary_dict() if summary else project.to_simplified_dict()
            for project in page.projects
        ],
        "count": len(page.projects),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
                "cross-project search scoped to this project, a slightly wider "
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
                "Maximum number of repositories to return in this window. If "
                "more exist than are returned, the response sets 'truncated' to "
                "true and 'next_page_start' to the cursor for the next call."
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
                "name) instead of the full record, for scanning a large list to "
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
            cross-project search scoped to this project, a slightly wider
            visibility surface than the unfiltered listing.
        start: Pagination cursor (offset of the first repository); 0 for the
            first window, else a prior response's ``next_page_start``.
        limit: Maximum number of repositories to return in this window.
        summary: When true, repository records carry identity fields only.

    Returns:
        JSON string with the list of repositories plus ``count`` (the size of this
        window), ``is_last_page``, ``truncated``, and ``next_page_start``; the list is
        complete when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.list_repositories,
        project_key=project_key,
        name=name,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "repositories": [
            repo.to_summary_dict() if summary else repo.to_simplified_dict()
            for repo in page.repositories
        ],
        "count": len(page.repositories),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
                "recently modified first). Any other value is rejected."
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
                "Maximum number of branches to return in this window. If more "
                "exist than are returned, the response sets 'truncated' to true "
                "and 'next_page_start' to the cursor for the next call."
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
                "latest_commit) instead of the full record, for scanning a large "
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
        order_by: Optional ordering (ALPHABETICAL or MODIFICATION).
        boost_matches: When true, floats exact/prefix filter matches up.
        start: Pagination cursor (offset of the first branch); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of branches to return in this window.
        summary: When true, branch records carry triage fields only.

    Returns:
        JSON string with the list of branches plus ``count`` (the size of this window),
        ``is_last_page``, ``truncated``, and ``next_page_start``; the list is complete
        when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.list_branches,
        project_key=project_key,
        repository_slug=repository_slug,
        filter_text=filter_text,
        order_by=order_by,
        boost_matches=boost_matches,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "branches": [
            branch.to_summary_dict() if summary else branch.to_simplified_dict()
            for branch in page.branches
        ],
        "count": len(page.branches),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
                "recently modified first). Any other value is rejected."
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
                "Maximum number of tags to return in this window. If more exist "
                "than are returned, the response sets 'truncated' to true and "
                "'next_page_start' to the cursor for the next call."
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
                "latest_commit) instead of the full record, for scanning a large "
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
        order_by: Optional ordering (ALPHABETICAL or MODIFICATION).
        start: Pagination cursor (offset of the first tag); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of tags to return in this window.
        summary: When true, tag records carry triage fields only.

    Returns:
        JSON string with the list of tags plus ``count`` (the size of this window),
        ``is_last_page``, ``truncated``, and ``next_page_start``; the list is complete
        when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.list_tags,
        project_key=project_key,
        repository_slug=repository_slug,
        filter_text=filter_text,
        order_by=order_by,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "tags": [
            tag.to_summary_dict() if summary else tag.to_simplified_dict()
            for tag in page.tags
        ],
        "count": len(page.tags),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
                "The tag name (e.g. 'v1.0.0' or 'release/1.0'). Names "
                "containing slashes are accepted; the name is percent-encoded "
                "per path component in the request path, so a slash is a "
                "path separator, not part of a component."
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
        JSON string with the tag: display id, ref id, latest commit, and the
        annotated-tag ``hash`` when present.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    tag = await run_bitbucket_fetcher_call(
        bitbucket.get_tag,
        project_key=project_key,
        repository_slug=repository_slug,
        name=name,
    )
    return json.dumps(tag.to_simplified_dict(), indent=2, ensure_ascii=False)


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
        id, and type). The minimal ref carries no commit SHA.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    branch = await run_bitbucket_fetcher_call(
        bitbucket.get_default_branch,
        project_key=project_key,
        repository_slug=repository_slug,
    )
    return json.dumps(branch.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Create Bitbucket Branch"},
)
@check_write_access
async def create_branch(
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
                "The short branch name (e.g. 'feature/x'), without a 'refs/' "
                "prefix. The server adds 'refs/heads/'. Must be a valid git "
                "ref name (no '..', no leading '-', no whitespace, no "
                "trailing '.lock')."
            ),
        ),
    ],
    start_point: Annotated[
        str,
        Field(
            description=(
                "The commit id or ref the branch starts from (e.g. 'main', "
                "'refs/tags/v1.0', or a 40-character commit id)."
            ),
        ),
    ],
    message: Annotated[
        str | None,
        Field(
            description=(
                "Optional message recorded with the ref change (at most 32768 "
                "characters)."
            ),
        ),
    ] = None,
) -> str:
    """Create a branch in a Bitbucket Data Center repository.

    One request. The start point is sent as given and resolved by the server.
    The response is confirmed against the request: its display id must equal
    'name' and its
    latest commit must be a commit id (equal to 'start_point' when that was a
    full commit id). Requires REPO_WRITE on the repository. The documented
    example configuration grants only REPO_READ, so set
    BITBUCKET_OAUTH_SCOPE=REPO_WRITE (or use a token issued with it). Blocked
    when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        name: The short branch name.
        start_point: The commit id or ref to branch from.
        message: Optional message recorded with the ref change.

    Returns:
        JSON string with the created branch: display id, ref id, latest
        commit, and type.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    branch = await run_bitbucket_fetcher_call(
        bitbucket.create_branch,
        project_key=project_key,
        repository_slug=repository_slug,
        name=name,
        start_point=start_point,
        message=message,
    )
    return json.dumps(branch.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Delete Bitbucket Branch", "destructiveHint": True},
)
@check_write_access
async def delete_branch(
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
                "The branch name, short (e.g. 'feature/x') or fully qualified "
                "('refs/heads/feature/x'). A short name is sent as "
                "'refs/heads/<name>' so the delete cannot address a tag."
            ),
        ),
    ],
    end_point: Annotated[
        str,
        Field(
            description=(
                "The full 40-character commit id the branch is expected to "
                "point at (its 'latest_commit' from list_branches). The server "
                "refuses the delete with a 400 when the branch points elsewhere."
            ),
        ),
    ],
    dry_run: Annotated[
        bool,
        Field(
            description=(
                "When true the server performs a dry run and deletes nothing."
            ),
        ),
    ] = False,
) -> str:
    """Delete a branch in a Bitbucket Data Center repository.

    The delete is conditional on 'end_point': read the branch's current
    'latest_commit' with list_branches first and pass it here. The server
    refuses (400) when the branch has moved, so a push between the read and
    the delete cannot be lost. HTTP 204 acknowledges the request even when
    the branch did not exist. The result reports the request as accepted with
    a note. List branches afterwards to verify the resulting state. The
    default branch cannot be deleted (400). Requires REPO_WRITE on the
    repository and branch permission on the name. The documented example
    configuration grants only REPO_READ, so set
    BITBUCKET_OAUTH_SCOPE=REPO_WRITE (or use a token issued with it). Blocked
    when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        name: The branch name.
        end_point: The full commit id the branch must currently point at.
        dry_run: When true, validate only.

    Returns:
        JSON string with the fully-qualified 'branch' id that was sent, the
        'end_point', 'dry_run', 'accepted' (true: the server answered 204),
        and a 'note' on verifying the result.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    result = await run_bitbucket_fetcher_call(
        bitbucket.delete_branch,
        project_key=project_key,
        repository_slug=repository_slug,
        name=name,
        end_point=end_point,
        dry_run=dry_run,
    )
    return json.dumps(result, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Create Bitbucket Tag"},
)
@check_write_access
async def create_tag(
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
                "The short tag name (e.g. 'v1.2.0'), without a 'refs/' prefix. "
                "The server adds 'refs/tags/'. Must be a valid git ref name "
                "(no '..', no leading '-', no whitespace, no trailing '.lock')."
            ),
        ),
    ],
    start_point: Annotated[
        str,
        Field(
            description=(
                "The commit id or ref the tag points at (e.g. 'main' or a "
                "40-character commit id)."
            ),
        ),
    ],
    message: Annotated[
        str | None,
        Field(
            description=(
                "Optional annotation message (at most 32768 characters). When "
                "given the tag is annotated. Otherwise it is lightweight."
            ),
        ),
    ] = None,
) -> str:
    """Create a tag in a Bitbucket Data Center repository.

    One request. The start point is sent as given and resolved by the server.
    The response is confirmed against the request: its display id must equal
    'name' and its
    latest commit must be a commit id (equal to 'start_point' when that was a
    full commit id). Requires REPO_WRITE on the repository. The documented
    example configuration grants only REPO_READ, so set
    BITBUCKET_OAUTH_SCOPE=REPO_WRITE (or use a token issued with it). Blocked
    when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        name: The short tag name.
        start_point: The commit id or ref to tag.
        message: Optional annotation message (makes an annotated tag).

    Returns:
        JSON string with the created tag: display id, ref id, latest commit,
        type, and the annotated-tag 'hash' when present.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    tag = await run_bitbucket_fetcher_call(
        bitbucket.create_tag,
        project_key=project_key,
        repository_slug=repository_slug,
        name=name,
        start_point=start_point,
        message=message,
    )
    return json.dumps(tag.to_simplified_dict(), indent=2, ensure_ascii=False)


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
                "Any other value is rejected."
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
                "Maximum number of commits to return in this window. If more "
                "exist than are returned, the response sets 'truncated' to true "
                "and 'next_page_start' to the cursor for the next call."
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
                "full record, for scanning a large history to pick one."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List commits in a Bitbucket Data Center repository.

    The history filters ('since', 'until', 'path', 'merges', 'follow_renames')
    are applied server-side. 'follow_renames' is only valid together with a
    single-file 'path'.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        since: Optional exclusive lower-bound commit/ref.
        until: Optional inclusive upper-bound commit/ref (e.g. a branch tip).
        path: Optional path to restrict the history to.
        merges: Optional merge-commit handling (exclude, include, or only).
        follow_renames: When true, follow a file across renames (needs 'path').
        ignore_missing: When true, ignore missing commits rather than failing.
        start: Pagination cursor (offset of the first commit); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of commits to return in this window.
        summary: When true, commit records carry triage fields only.

    Returns:
        JSON string with the list of commits plus ``count`` (the size of this window),
        ``is_last_page``, ``truncated``, and ``next_page_start``; the list is complete
        when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.list_commits,
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
        "commits": [
            commit.to_summary_dict() if summary else commit.to_simplified_dict()
            for commit in page.commits
        ],
        "count": len(page.commits),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
        JSON string with the commit: id, display id, message, author/committer,
        timestamps, and parents.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    commit = await run_bitbucket_fetcher_call(
        bitbucket.get_commit,
        project_key=project_key,
        repository_slug=repository_slug,
        commit_id=commit_id,
    )
    return json.dumps(commit.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Get Bitbucket Commit Build Status", "readOnlyHint": True},
)
async def get_commit_build_status(
    ctx: Context,
    commit_id: Annotated[
        str,
        Field(
            description=(
                "The full 40-character commit SHA. Abbreviated ids are rejected "
                "because this lookup is not repository scoped and cannot resolve "
                "them. For a pull request's head commit, use "
                "'from_ref.latest_commit' from get_pull_request."
            ),
        ),
    ],
    order_by: Annotated[
        str | None,
        Field(
            description=(
                "Optional ordering: 'NEWEST' (most recently updated first), "
                "'OLDEST', or 'STATUS' (grouped by state). Any other value is "
                "rejected."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first status to return. "
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
                "Maximum number of statuses to return in this window. If more "
                "exist than are returned, the response sets 'truncated' to true "
                "and 'next_page_start' to the cursor for the next call. The "
                "instance serves at most the 100 most recent statuses per commit."
            ),
            default=DEFAULT_BUILD_STATUSES_LIMIT,
            ge=1,
            le=MAX_BUILD_STATUSES_LIMIT,
        ),
    ] = DEFAULT_BUILD_STATUSES_LIMIT,
) -> str:
    """Get the CI build statuses posted against a commit.

    Each status carries a 'state' (normally SUCCESSFUL, FAILED, INPROGRESS,
    CANCELLED, or UNKNOWN), the CI plan key and name, the build URL, and
    optional test counts. The lookup is by commit id alone (not repository
    scoped); to check a pull request's CI state, pass the
    'from_ref.latest_commit' value returned by get_pull_request. An empty
    list means no status was posted against that exact SHA; it does not
    confirm the commit exists, so verify the SHA with get_commit when that
    matters. The endpoint returns at most 100 statuses per commit.

    Args:
        ctx: The FastMCP context.
        commit_id: The commit SHA.
        order_by: Optional ordering (NEWEST, OLDEST, or STATUS).
        start: Pagination cursor (offset of the first status); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of statuses to return in this window.

    Returns:
        JSON string with the list of build statuses plus ``count`` (the size
        of this window), ``page_counts`` (statuses in this window per state,
        with a status that has no state tallied under UNKNOWN),
        ``is_last_page``, ``truncated``, and ``next_page_start``.
        ``page_counts`` covers only the returned window. It covers every
        status the endpoint returns for the commit only when the call used
        ``start=0`` and came back with ``is_last_page`` true and ``truncated``
        false, and the endpoint itself returns at most 100 statuses.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.get_commit_build_statuses,
        commit_id=commit_id,
        order_by=order_by,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "commit_id": commit_id.strip().lower(),
        "build_statuses": [status.to_simplified_dict() for status in page.statuses],
        "count": len(page.statuses),
        "page_counts": page.page_counts,
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
    one call. The response's 'type' field ("FILE" or "DIRECTORY") tells which.
    'at' selects a commit/branch/tag (the default branch otherwise). Page
    large files or directories by calling again with 'start' set to the returned
    'next_page_start'. '.'/'..' and traversal paths are rejected. 'count' is the
    number of items in this window.

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
        ``next_page_start``.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    result = await run_bitbucket_fetcher_call(
        bitbucket.browse,
        project_key=project_key,
        repository_slug=repository_slug,
        path=path,
        at=at,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
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
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Compare Bitbucket Changes", "readOnlyHint": True},
)
async def compare_changes(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    from_ref: Annotated[
        str,
        Field(
            description=(
                "The source commit or ref whose changes are reported: a full or "
                "partial commit SHA, or a branch or tag name (e.g. 'feature/x', "
                "'refs/tags/v1.2')."
            ),
            min_length=1,
        ),
    ],
    to_ref: Annotated[
        str | None,
        Field(
            description=(
                "The target commit or ref to compare against. Omit for the "
                "repository's default branch."
            ),
            default=None,
        ),
    ] = None,
    from_repo: Annotated[
        str | None,
        Field(
            description=(
                "The repository holding 'from_ref' when it is a fork of this "
                "one, as 'PROJECT/slug' (e.g. 'FORK/my-repo'). Omit when "
                "'from_ref' lives in this repository."
            ),
            default=None,
        ),
    ] = None,
    start: Annotated[
        int,
        Field(
            description=(
                "Pagination cursor: the offset of the first changed file to "
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
                "Maximum number of changed files to return in this window. If "
                "more exist than are returned, the response sets 'truncated' to "
                "true and 'next_page_start' to the cursor for the next call."
            ),
            default=DEFAULT_CHANGES_LIMIT,
            ge=1,
            le=MAX_CHANGES_LIMIT,
        ),
    ] = DEFAULT_CHANGES_LIMIT,
) -> str:
    """List the files that differ between two refs of a Bitbucket repository.

    Reports the changes present in 'from_ref' but not in 'to_ref' (the
    repository's default branch when omitted), e.g. what a release branch
    adds on top of a tag. Each entry carries the file's 'path', its
    'src_path' for a move or copy, the change 'type' (ADD, COPY, DELETE,
    MODIFY, MOVE, UNKNOWN), and its 'node_type'. Use it to pick files for
    bitbucket_compare_diff with 'path' (and 'src_path') when the whole diff
    is too large to download. An empty result usually means 'from_ref' is already
    contained in 'to_ref'. Swap the two to see the reverse direction.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        from_ref: The source commit or ref.
        to_ref: The target commit or ref; the default branch when omitted.
        from_repo: The fork holding 'from_ref', as 'PROJECT/slug'.
        start: Pagination cursor (offset of the first file); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of changed files to return in this window.

    Returns:
        JSON string with the list of ``changes`` plus ``count`` (the size of this
        window), ``is_last_page``, ``truncated``, and ``next_page_start``. The
        list is complete when ``is_last_page`` is true and ``truncated`` is
        false. A null ``next_page_start`` with ``truncated`` true cannot be
        resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.compare_changes,
        project_key=project_key,
        repository_slug=repository_slug,
        from_ref=from_ref,
        to_ref=to_ref,
        from_repo=from_repo,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "changes": [change.to_simplified_dict() for change in page.changes],
        "count": len(page.changes),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
    }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Compare Bitbucket Commits", "readOnlyHint": True},
)
async def compare_commits(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    from_ref: Annotated[
        str,
        Field(
            description=(
                "The source commit or ref whose commits are reported: a full or "
                "partial commit SHA, or a branch or tag name (e.g. 'feature/x', "
                "'refs/tags/v1.2')."
            ),
            min_length=1,
        ),
    ],
    to_ref: Annotated[
        str | None,
        Field(
            description=(
                "The target commit or ref to compare against. Omit for the "
                "repository's default branch."
            ),
            default=None,
        ),
    ] = None,
    from_repo: Annotated[
        str | None,
        Field(
            description=(
                "The repository holding 'from_ref' when it is a fork of this "
                "one, as 'PROJECT/slug' (e.g. 'FORK/my-repo'). Omit when "
                "'from_ref' lives in this repository."
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
                "Maximum number of commits to return in this window. If more "
                "exist than are returned, the response sets 'truncated' to true "
                "and 'next_page_start' to the cursor for the next call."
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
                "When true, return a compact projection per commit (id, "
                "display id, author, timestamp, first message line) for "
                "scanning many commits cheaply."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List the commits reachable from one ref but not from another.

    Reports the commits reachable from 'from_ref' that are not reachable
    from 'to_ref' (the repository's default branch when omitted), e.g. the
    commits a branch adds since it diverged, or everything since a release
    tag. Each commit carries its id, message, author, committer, timestamps,
    and parents. An empty result usually means 'from_ref' is already
    contained in 'to_ref'. Swap the two to see the reverse direction.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        from_ref: The source commit or ref.
        to_ref: The target commit or ref; the default branch when omitted.
        from_repo: The fork holding 'from_ref', as 'PROJECT/slug'.
        start: Pagination cursor (offset of the first commit); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of commits to return in this window.
        summary: When true, commit records carry triage fields only.

    Returns:
        JSON string with the list of ``commits`` plus ``count`` (the size of this
        window), ``is_last_page``, ``truncated``, and ``next_page_start``. The
        list is complete when ``is_last_page`` is true and ``truncated`` is
        false. A null ``next_page_start`` with ``truncated`` true cannot be
        resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.compare_commits,
        project_key=project_key,
        repository_slug=repository_slug,
        from_ref=from_ref,
        to_ref=to_ref,
        from_repo=from_repo,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "commits": [
            commit.to_summary_dict() if summary else commit.to_simplified_dict()
            for commit in page.commits
        ],
        "count": len(page.commits),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
    }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Compare Bitbucket Diff", "readOnlyHint": True},
)
async def compare_diff(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The repository slug (e.g. 'my-repo').")
    ],
    from_ref: Annotated[
        str,
        Field(
            description=(
                "The source commit or ref whose changes are reported: a full or "
                "partial commit SHA, or a branch or tag name (e.g. 'feature/x', "
                "'refs/tags/v1.2')."
            ),
            min_length=1,
        ),
    ],
    to_ref: Annotated[
        str | None,
        Field(
            description=(
                "The target commit or ref to compare against. Omit for the "
                "repository's default branch."
            ),
            default=None,
        ),
    ] = None,
    from_repo: Annotated[
        str | None,
        Field(
            description=(
                "The repository holding 'from_ref' when it is a fork of this "
                "one, as 'PROJECT/slug' (e.g. 'FORK/my-repo'). Omit when "
                "'from_ref' lives in this repository."
            ),
            default=None,
        ),
    ] = None,
    max_lines_per_file: Annotated[
        int,
        Field(
            description=(
                "Maximum diff lines to return per file (token control). Files "
                "exceeding this are truncated. The response flags the file with "
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
                "the comparison changes more files than this, the response "
                "returns the first 'max_files' and sets 'truncated' to true. "
                "'total_files' reports the full count."
            ),
            default=DEFAULT_MAX_FILES,
            ge=1,
            le=MAX_MAX_FILES,
        ),
    ] = DEFAULT_MAX_FILES,
    context_lines: Annotated[
        int | None,
        Field(
            description=(
                "Number of unchanged context lines to include around each "
                "change, applied by the server before download. Omit for the "
                "server default (10). Lower it to shrink a large diff."
            ),
            default=None,
            ge=0,
            le=MAX_CONTEXT_LINES,
        ),
    ] = None,
    path: Annotated[
        str | None,
        Field(
            description=(
                "Diff a single file at this path instead of the whole "
                "comparison. Use bitbucket_compare_changes to list the paths. "
                "'.'/'..' and traversal paths are rejected."
            ),
            default=None,
        ),
    ] = None,
    src_path: Annotated[
        str | None,
        Field(
            description=(
                "The file's previous path when it was moved, copied, or "
                "renamed (the 'src_path' of its changed-file entry). Only "
                "valid together with 'path'."
            ),
            default=None,
        ),
    ] = None,
    whitespace: Annotated[
        str | None,
        Field(
            description=(
                "Whitespace handling. The only accepted value is 'ignore-all', "
                "which drops whitespace-only changes from the diff. Omit for "
                "the server default (whitespace is significant)."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Get the structured diff between two refs of a Bitbucket repository.

    Reports the changes present in 'from_ref' but not in 'to_ref' (the
    repository's default branch when omitted) as a structured hunk model
    (files → hunks → segments → lines), not raw unified-diff text. The diff
    is bounded for token control: each file is capped at
    ``max_lines_per_file`` lines and the file list is capped at
    ``max_files``; the top-level ``truncated`` flag reports whether anything
    was omitted. A hunk whose lines all fell past the per-file cap is kept
    with its line coordinates and no lines, so the change's location
    survives. The raw download itself is capped at 10 MiB. When a comparison
    exceeds that, narrow the request: list its files with
    bitbucket_compare_changes and fetch one at a time with 'path', or lower
    'context_lines'. An empty result usually means 'from_ref' is already
    contained in 'to_ref'. Swap the two to see the reverse direction.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        from_ref: The source commit or ref.
        to_ref: The target commit or ref; the default branch when omitted.
        from_repo: The fork holding 'from_ref', as 'PROJECT/slug'.
        max_lines_per_file: Per-file diff-line cap.
        max_files: Maximum number of changed files to return.
        context_lines: Server-side context lines around each change.
        path: When set, diff only this file.
        src_path: The file's previous path for a move or rename (with path).
        whitespace: 'ignore-all' to drop whitespace-only changes.

    Returns:
        JSON string with the structured diff: ``files``, ``count``,
        ``total_files``, and ``truncated``.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    diff = await run_bitbucket_fetcher_call(
        bitbucket.compare_diff,
        project_key=project_key,
        repository_slug=repository_slug,
        from_ref=from_ref,
        to_ref=to_ref,
        from_repo=from_repo,
        max_lines_per_file=max_lines_per_file,
        max_files=max_files,
        context_lines=context_lines,
        path=path,
        src_path=src_path,
        whitespace=whitespace,
    )
    return json.dumps(diff.to_simplified_dict(), indent=2, ensure_ascii=False)


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
                "'MERGED', or 'ALL'. Any other value is rejected."
            ),
            default=None,
        ),
    ] = None,
    direction: Annotated[
        str | None,
        Field(
            description=(
                "Direction relative to the repository: 'INCOMING' (default) or "
                "'OUTGOING'. Any other value is rejected."
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
            description=(
                "Ordering: 'NEWEST' (default) or 'OLDEST'. Any other value is rejected."
            ),
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
                "title, state, author, and the target repository's project_key "
                "and repository_slug) instead of the full record, for scanning "
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
        draft: Optional filter by draft status, sent to the server as the
            lowercase string 'true'/'false'.
        start: Pagination cursor (offset of the first pull request); 0 for the
            first window, else a prior response's ``next_page_start``.
        limit: Maximum number of pull requests to return in this window.
        summary: When true, pull-request records carry triage fields only.

    Returns:
        JSON string with the list of pull requests plus ``count`` (the size of this
        window), ``is_last_page``, ``truncated``, and ``next_page_start``; the list is
        complete when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.list_pull_requests,
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
        "pull_requests": [
            pr.to_summary_dict() if summary else pr.to_simplified_dict()
            for pr in page.pull_requests
        ],
        "count": len(page.pull_requests),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
    }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "List Bitbucket Pull Requests for a User",
        "readOnlyHint": True,
    },
)
async def list_user_pull_requests(
    ctx: Context,
    user: Annotated[
        str | None,
        Field(
            description=(
                "Bitbucket username whose pull requests to list. Defaults to "
                "the authenticated user (the identity behind the server's "
                "Bitbucket credentials), so a call without it answers 'my pull "
                "requests'. Results are still limited to what the caller may see."
            ),
            default=None,
        ),
    ] = None,
    role: Annotated[
        str | None,
        Field(
            description=(
                "Filter by the user's role on each pull request: 'REVIEWER', "
                "'AUTHOR', or 'PARTICIPANT'. Omit for any role. Any other value "
                "is rejected."
            ),
            default=None,
        ),
    ] = None,
    participant_status: Annotated[
        list[str] | None,
        Field(
            description=(
                "Filter by the user's participant status: any of 'UNAPPROVED', "
                "'NEEDS_WORK', 'APPROVED'. Omit for any status. Any other value "
                "is rejected."
            ),
            default=None,
        ),
    ] = None,
    state: Annotated[
        str | None,
        Field(
            description=(
                "Filter by pull-request state: 'OPEN', 'DECLINED', or 'MERGED'. "
                "Omit for any state. Any other value is rejected."
            ),
            default=None,
        ),
    ] = None,
    order: Annotated[
        str | None,
        Field(
            description=(
                "Ordering: 'NEWEST' (default), 'OLDEST', 'DRAFT_STATUS', "
                "'PARTICIPANT_STATUS', or 'CLOSED_DATE'. Any other value is "
                "rejected."
            ),
            default=None,
        ),
    ] = None,
    closed_since: Annotated[
        int | None,
        Field(
            description=(
                "Only pull requests closed within the last N seconds (e.g. 86400 "
                "for the previous 24 hours). Omit for no closed-date window."
            ),
            default=None,
            ge=1,
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
                "title, state, author, and the target repository's project_key "
                "and repository_slug) instead of the full record, for scanning "
                "a large list to pick one before fetching its full detail."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List the pull requests a user is involved in, across all repositories.

    Answers "what is waiting for my review" and "what do I have open" in one
    server-filtered request to the Bitbucket Data Center dashboard endpoint,
    without walking repositories.

    Args:
        ctx: The FastMCP context.
        user: Optional Bitbucket username; defaults to the authenticated user.
        role: Optional role filter (reviewer, author, or participant).
        participant_status: Optional participant statuses to match.
        state: Optional pull-request state filter.
        order: Optional ordering.
        closed_since: Optional window in seconds on the closed date.
        start: Pagination cursor (offset of the first pull request); 0 for the
            first window, else a prior response's ``next_page_start``.
        limit: Maximum number of pull requests to return in this window.
        summary: When true, pull-request records carry triage fields only.

    Returns:
        JSON string with the list of pull requests plus ``count`` (the size of this
        window), ``is_last_page``, ``truncated``, and ``next_page_start``. The
        list is complete when ``is_last_page`` is true and ``truncated`` is
        false. A null ``next_page_start`` with ``truncated`` true cannot be
        resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.list_user_pull_requests,
        user=user,
        role=role,
        participant_status=participant_status,
        state=state,
        order=order,
        closed_since=closed_since,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "pull_requests": [
            pr.to_summary_dict() if summary else pr.to_simplified_dict()
            for pr in page.pull_requests
        ],
        "count": len(page.pull_requests),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
        JSON string with the pull request: metadata, refs, and
        reviewer/approval state.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    pull_request = await run_bitbucket_fetcher_call(
        bitbucket.get_pull_request,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
    )
    return json.dumps(pull_request.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Get Bitbucket Pull Request Merge Status",
        "readOnlyHint": True,
    },
)
async def get_pull_request_merge_status(
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
    """Check whether a Bitbucket Data Center pull request can be merged.

    Reports conflicts between the source and target branches and the merge
    checks (required reviewers, required builds, and other repository hooks)
    that veto the merge. Only an open pull request can be checked. A merged
    or declined one fails with the server's message.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.

    Returns:
        JSON string with ``can_merge`` (when the server reports it),
        ``conflicted``, ``outcome`` (``CLEAN``, ``CONFLICTED``, or
        ``UNKNOWN`` when the server has not determined it yet, in which case
        check again shortly; any other server value is passed through as
        reported), and ``vetoes`` (each with ``summary`` and ``detail``;
        empty when nothing blocks the merge).
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    merge_status = await run_bitbucket_fetcher_call(
        bitbucket.get_pull_request_merge_status,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
    )
    return json.dumps(merge_status.to_simplified_dict(), indent=2, ensure_ascii=False)


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
                "Maximum number of commits to return in this window. If more "
                "exist than are returned, the response sets 'truncated' to true "
                "and 'next_page_start' to the cursor for the next call."
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
        summary: When true, commit records carry triage fields only.

    Returns:
        JSON string with the list of commits plus ``count`` (the size of this window),
        ``is_last_page``, ``truncated``, and ``next_page_start``; the list is complete
        when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.get_pull_request_commits,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "commits": [
            commit.to_summary_dict() if summary else commit.to_simplified_dict()
            for commit in page.commits
        ],
        "count": len(page.commits),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
    }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Get Bitbucket Pull Request Changes",
        "readOnlyHint": True,
    },
)
async def get_pull_request_changes(
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
                "Pagination cursor: the offset of the first changed file to "
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
                "Maximum number of changed files to return in this window. If "
                "more exist than are returned, the response sets 'truncated' to "
                "true and 'next_page_start' to the cursor for the next call."
            ),
            default=DEFAULT_CHANGES_LIMIT,
            ge=1,
            le=MAX_CHANGES_LIMIT,
        ),
    ] = DEFAULT_CHANGES_LIMIT,
) -> str:
    """List the files a Bitbucket Data Center pull request changes.

    Each entry carries the file's 'path', its 'src_path' for a move or copy,
    the change 'type' (ADD, COPY, DELETE, MODIFY, MOVE, UNKNOWN), and its
    'node_type'. Use it to pick files for bitbucket_get_pull_request_diff with
    'path' (and 'src_path') when the whole diff is too large to download.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        start: Pagination cursor (offset of the first file); 0 for the first
            window, else a prior response's ``next_page_start``.
        limit: Maximum number of changed files to return in this window.

    Returns:
        JSON string with the list of ``changes`` plus ``count`` (the size of this
        window), ``is_last_page``, ``truncated``, and ``next_page_start``; the list is
        complete when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.get_pull_request_changes,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "changes": [change.to_simplified_dict() for change in page.changes],
        "count": len(page.changes),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
                "the diff holds more files than this, the response returns the "
                "first 'max_files' and sets 'truncated' to true. 'total_files' "
                "counts the files in the diff Bitbucket returned, which a "
                "'path' filter or Bitbucket's own limits may already have "
                "reduced."
            ),
            default=DEFAULT_MAX_FILES,
            ge=1,
            le=MAX_MAX_FILES,
        ),
    ] = DEFAULT_MAX_FILES,
    context_lines: Annotated[
        int | None,
        Field(
            description=(
                "Number of unchanged context lines to include around each "
                "change, applied by the server before download. Omit for the "
                "server default (10). Lower it to shrink a large diff."
            ),
            default=None,
            ge=0,
            le=MAX_CONTEXT_LINES,
        ),
    ] = None,
    path: Annotated[
        str | None,
        Field(
            description=(
                "Diff a single file at this path instead of the whole pull "
                "request. Use bitbucket_get_pull_request_changes to list the "
                "paths. "
                "'.'/'..' and traversal paths are rejected."
            ),
            default=None,
        ),
    ] = None,
    src_path: Annotated[
        str | None,
        Field(
            description=(
                "The file's previous path when it was moved, copied, or "
                "renamed (the 'src_path' of its changed-file entry). Only "
                "valid together with 'path'."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Get a Bitbucket Data Center pull request's structured diff.

    Returns the diff as a structured hunk model (files → hunks → segments →
    lines), not raw unified-diff text. The diff is bounded for token control:
    each file is capped at ``max_lines_per_file`` lines and the file list is
    capped at ``max_files``; the top-level ``truncated`` flag reports whether
    anything was omitted. A hunk whose lines all fell past the per-file cap is
    kept with its line coordinates and no lines, so the change's location
    survives. The raw download itself is capped at 10 MiB. When a
    pull request's diff exceeds that, narrow the request: list its files with
    bitbucket_get_pull_request_changes and fetch one at a time with 'path', or
    lower 'context_lines'.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        max_lines_per_file: Per-file diff-line cap.
        max_files: Maximum number of changed files to return.
        context_lines: Server-side context lines around each change.
        path: When set, diff only this file.
        src_path: The file's previous path for a move or rename (with path).

    Returns:
        JSON string with the structured diff: ``files``, ``count``,
        ``total_files``, and ``truncated``.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    diff = await run_bitbucket_fetcher_call(
        bitbucket.get_pull_request_diff,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        max_lines_per_file=max_lines_per_file,
        max_files=max_files,
        context_lines=context_lines,
        path=path,
        src_path=src_path,
    )
    return json.dumps(diff.to_simplified_dict(), indent=2, ensure_ascii=False)


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
        JSON string with the activity timeline plus ``count`` (the size of this window),
        ``is_last_page``, ``truncated``, and ``next_page_start``; the timeline is
        complete when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.get_activities,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        start=start,
        limit=limit,
    )
    response_data: dict[str, object] = {
        "activities": [a.to_simplified_dict() for a in page.activities],
        "count": len(page.activities),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
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
                "Maximum number of activity entries fetched for this window. "
                "The comment filter applies to the fetched window, so fewer "
                "comments can be returned. If more entries exist than were "
                "fetched, the response sets 'truncated' to true and "
                "'next_page_start' to the cursor for the next call."
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
        limit: Maximum number of activity entries fetched for this window;
            the comment filter applies to that window.

    Returns:
        JSON string with the comments plus ``count`` (the size of this window),
        ``is_last_page``, ``truncated``, and ``next_page_start``; the comments are
        complete when ``is_last_page`` is true and ``truncated`` is false, and a null
        ``next_page_start`` with ``truncated`` true cannot be resumed.

        An empty window with ``is_last_page`` false can still precede comments.
        Each call issues one upstream request and the comment filter narrows
        that activity window, so a window can hold zero comments while more of
        the timeline remains. Keep paging with ``start=next_page_start`` until
        ``is_last_page`` is true before concluding the pull request has no
        further comments.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    page = await run_bitbucket_fetcher_call(
        bitbucket.get_activities,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        action="COMMENTED",
        start=start,
        limit=limit,
    )
    comments = [
        a.comment.to_simplified_dict() for a in page.activities if a.comment is not None
    ]
    response_data: dict[str, object] = {
        "comments": comments,
        "count": len(comments),
        "is_last_page": page.is_last_page,
        "truncated": page.truncated,
        "next_page_start": page.next_page_start,
    }
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Create Bitbucket Pull Request",
        "destructiveHint": False,
    },
)
@check_write_access
async def create_pull_request(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The target Bitbucket project key (e.g. 'PROJ').")
    ],
    repository_slug: Annotated[
        str, Field(description="The target repository slug (e.g. 'my-repo').")
    ],
    title: Annotated[
        str,
        Field(
            description=(
                "The pull-request title. One non-blank line of at most "
                f"{MAX_PR_TITLE_CHARS} characters."
            ),
        ),
    ],
    from_ref: Annotated[
        str,
        Field(
            description=(
                "The source branch or tag. A bare name ('feature/x') is sent as "
                "'refs/heads/feature/x'. A value starting with 'refs/' (for "
                "example 'refs/tags/v1.2') is sent as given."
            ),
        ),
    ],
    to_ref: Annotated[
        str,
        Field(
            description=(
                "The target branch. A bare name ('main') is sent as "
                "'refs/heads/main'. A 'refs/heads/...' value is sent as given. "
                "Tags are not valid targets."
            ),
        ),
    ],
    description: Annotated[
        str | None,
        Field(
            description=(
                "The pull-request description (Markdown) of at most "
                f"{MAX_PR_DESCRIPTION_CHARS} characters."
            ),
            default=None,
        ),
    ] = None,
    draft: Annotated[
        bool | None,
        Field(
            description=(
                "Create the pull request as a draft. Omit to leave the server "
                "default (not a draft). Needs a Data Center version with draft "
                "pull requests. The flag is confirmed against the response."
            ),
            default=None,
        ),
    ] = None,
    reviewers: Annotated[
        list[str] | None,
        Field(
            description=(
                "User names to add as reviewers (the 'name' field of "
                "get_current_user, not the display name). Repeated names are "
                "sent once. A name the server cannot resolve fails the whole "
                "request with a 409."
            ),
            default=None,
        ),
    ] = None,
    from_repo: Annotated[
        str | None,
        Field(
            description=(
                "The repository holding 'from_ref' when it is not the target "
                "repository, as 'PROJECT/slug'. Must be a fork in the same "
                "hierarchy as the target. Omit for a same-repository pull "
                "request."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Create a pull request (needs REPO_READ on the source and target repositories).

    One request. Refs and reviewers are sent as given and resolved by the
    server. Bare ref names are qualified as 'refs/heads/<name>'. The source
    may be a branch or a tag. The target must be a branch. The source may
    live in a fork of the target repository ('from_repo'). Reviewers are
    given by user name.

    REPO_READ is the scope the documented OAuth setup grants. A 409 carries the
    server's reason: an unresolved reviewer, source and target being the same
    ref, the target already containing every source commit, an existing pull
    request between the refs, or an archived target repository. A 404 names
    the refs that were sent. An unconfirmed-write error means the server
    replied 201 with a body that differs from the request. List the open
    pull requests before retrying. Blocked when the server runs with
    READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The target project key.
        repository_slug: The target repository slug.
        title: The pull-request title; must be non-blank.
        from_ref: The source branch or tag.
        to_ref: The target branch.
        description: Optional description (Markdown).
        draft: Optional draft flag.
        reviewers: Optional reviewer user names.
        from_repo: Optional 'PROJECT/slug' of the fork holding the source.

    Returns:
        JSON string with the created pull request: its 'id', 'version',
        'title', 'state', refs, author, and reviewers.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    pull_request = await run_bitbucket_fetcher_call(
        bitbucket.create_pull_request,
        project_key=project_key,
        repository_slug=repository_slug,
        title=title,
        from_ref=from_ref,
        to_ref=to_ref,
        description=description,
        draft=draft,
        reviewers=reviewers,
        from_repo=from_repo,
    )
    return json.dumps(pull_request.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Update Bitbucket Pull Request",
        "destructiveHint": True,
        "idempotentHint": False,
    },
)
@check_write_access
async def update_pull_request(
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
    version: Annotated[
        int,
        Field(
            description=(
                "The pull request's current version, read from "
                "get_pull_request immediately before updating. A 409 means "
                "the pull request changed since you read it. Re-read and retry."
            ),
            ge=0,
        ),
    ],
    title: Annotated[
        str | None,
        Field(
            description=(
                "A new title: one non-blank line of at most "
                f"{MAX_PR_TITLE_CHARS} characters. Omit to keep the current one."
            ),
            default=None,
        ),
    ] = None,
    description: Annotated[
        str | None,
        Field(
            description=(
                "A new description (Markdown) of at most "
                f"{MAX_PR_DESCRIPTION_CHARS} characters; it replaces the whole "
                "text, and an empty string clears it. Omit to keep the current "
                "one."
            ),
            default=None,
        ),
    ] = None,
    draft: Annotated[
        bool | None,
        Field(
            description=(
                "Set (true) or clear (false) the draft flag. Omit to keep the "
                "current one. Needs a Data Center version with draft pull "
                "requests. The flag is confirmed against the response."
            ),
            default=None,
        ),
    ] = None,
    to_ref: Annotated[
        str | None,
        Field(
            description=(
                "A new target branch. A bare name ('main') is sent as "
                "'refs/heads/main'. A 'refs/heads/...' value is sent as given. "
                "Tags are not valid targets. Omit to keep the current one."
            ),
            default=None,
        ),
    ] = None,
    reviewers: Annotated[
        list[str] | None,
        Field(
            description=(
                "The complete new reviewer list as user names (the 'name' "
                "field of get_current_user). It replaces the current list, so "
                "include the reviewers to keep. An empty list removes every "
                "reviewer. Omit to keep the current list. A name the server "
                "cannot resolve fails the whole request with a 409."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Update pull-request metadata (needs REPO_WRITE, or REPO_READ as the author).

    Changes the title, description, draft flag, target branch, or reviewers
    of an existing pull request in one optimistic-locked request: 'version'
    must be the pull request's current version from get_pull_request, and
    at least one field to change must be given. Omitted fields keep their
    current value. An empty description clears it. 'reviewers' replaces the
    whole list: pass every reviewer to keep, or an empty list to remove them
    all. The author and the participants cannot be changed.

    The documented OAuth setup grants REPO_READ, which is enough only for
    the pull request's author. Anyone else needs REPO_WRITE, a scope that
    applies to every token the server issues. A 409 carries the server's
    reason: a stale version, a reviewer that could not be added, or a
    target-branch conflict (an open pull request to that branch already
    exists, it equals the source, or it already contains every source
    commit). An unconfirmed-write error means the server replied 200 with a
    body that differs from the request. Read the pull request again before
    retrying. Blocked when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        version: The pull request's current version (optimistic-lock token).
        title: Optional new title.
        description: Optional new description (Markdown).
        draft: Optional new draft flag.
        to_ref: Optional new target branch.
        reviewers: Optional complete reviewer list; empty to clear.

    Returns:
        JSON string with the updated pull request: its 'id', new 'version',
        'title', 'state', refs, author, and reviewers.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    pull_request = await run_bitbucket_fetcher_call(
        bitbucket.update_pull_request,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        version=version,
        title=title,
        description=description,
        draft=draft,
        to_ref=to_ref,
        reviewers=reviewers,
    )
    return json.dumps(pull_request.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Add Bitbucket Pull Request Comment",
        "destructiveHint": False,
    },
)
@check_write_access
async def add_pull_request_comment(
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
                "(a must-resolve item), valid in any comment mode."
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
        JSON string with the created comment: its 'id' and 'version', plus
        author/text/thread state.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    comment = await run_bitbucket_fetcher_call(
        bitbucket.add_comment,
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
    return json.dumps(comment.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Set Bitbucket Pull Request Review Status",
        "destructiveHint": True,
    },
)
@check_write_access
async def set_pull_request_review_status(
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

    Sets the caller's own participant status to 'APPROVED', 'NEEDS_WORK'
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
        JSON string with the confirmed review status.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    participant = await run_bitbucket_fetcher_call(
        bitbucket.set_review_status,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        status=status,
    )
    return json.dumps(participant, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Edit Bitbucket Pull Request Comment",
        "destructiveHint": True,
    },
)
@check_write_access
async def edit_pull_request_comment(
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
                "The comment's current version, from add_pull_request_comment or "
                "get_pull_request_comments. A 409 means the comment changed "
                "since you read it. Re-fetch its version and retry."
            ),
            ge=0,
        ),
    ],
) -> str:
    """Edit the text of a Bitbucket Data Center pull-request comment.

    Replaces the comment text via an optimistic-locked update: 'version' must be
    the comment's current version (from add_pull_request_comment or
    get_pull_request_comments). A 409 means the comment changed since you read
    it. Re-fetch the version and retry. Only the comment **author** may edit
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
        JSON string with the updated comment: its resulting 'version' and text.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    comment = await run_bitbucket_fetcher_call(
        bitbucket.update_comment,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        comment_id=comment_id,
        version=version,
        text=text,
    )
    return json.dumps(comment.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Resolve Bitbucket Pull Request Comment Thread",
        "destructiveHint": True,
    },
)
@check_write_access
async def resolve_pull_request_comment(
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
                "The comment's current version, from add_pull_request_comment or "
                "get_pull_request_comments. A 409 means the comment changed "
                "since you read it. Re-fetch its version and retry."
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
    'version' must be the comment's current version (from add_pull_request_comment or
    get_pull_request_comments). A 409 means the comment changed since you read
    it. Re-fetch the version and retry. This is thread resolution (the common
    review action), not task resolution. The comment id must be the thread's
    root comment; the resolved state belongs to the thread as a whole. Blocked
    when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        comment_id: The id of the comment whose thread to resolve.
        version: The comment's current version (optimistic-lock token).
        resolved: True (default) resolves the thread; False reopens it.

    Returns:
        JSON string with the updated comment: its resulting 'version' and
        'thread_resolved' state.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    comment = await run_bitbucket_fetcher_call(
        bitbucket.update_comment,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        comment_id=comment_id,
        version=version,
        thread_resolved=resolved,
    )
    return json.dumps(comment.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Resolve Bitbucket Pull Request Task",
        "destructiveHint": True,
    },
)
@check_write_access
async def resolve_pull_request_task(
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
        Field(
            description=(
                "The id of the task comment (a comment with severity BLOCKER)."
            ),
            ge=1,
        ),
    ],
    version: Annotated[
        int,
        Field(
            description=(
                "The comment's current version, from add_pull_request_comment or "
                "get_pull_request_comments. A 409 means the comment changed "
                "since you read it. Re-fetch its version and retry."
            ),
            ge=0,
        ),
    ],
    state: Annotated[
        str,
        Field(
            description=(
                "The task state to set: 'RESOLVED' (default) or 'OPEN' to "
                "reopen. Any other value is rejected."
            ),
            default="RESOLVED",
        ),
    ] = "RESOLVED",
) -> str:
    """Resolve or reopen a Bitbucket Data Center pull-request task (needs REPO_READ).

    Sets the task 'state' of a comment whose severity is BLOCKER via an
    optimistic-locked update: 'version' must be the comment's current version
    (from add_pull_request_comment or get_pull_request_comments). A 409 means
    the comment changed since you read it. Re-fetch the version and retry.
    This is task state (the must-do checklist item on a pull request), not
    thread resolution. Use resolve_pull_request_comment for the thread. The
    endpoint needs REPO_READ, and Bitbucket lets the comment author, the
    pull-request author, or a repository admin change the state. The result
    is confirmed against the request, so setting a state on a NORMAL comment
    that the server leaves unchanged is reported as an unconfirmed write.
    Blocked when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        comment_id: The id of the task comment.
        version: The comment's current version (optimistic-lock token).
        state: 'RESOLVED' (default) resolves the task; 'OPEN' reopens it.

    Returns:
        JSON string with the updated comment: its resulting 'version',
        'severity', and 'state'.

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    comment = await run_bitbucket_fetcher_call(
        bitbucket.set_task_state,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        comment_id=comment_id,
        version=version,
        state=state,
    )
    return json.dumps(comment.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Delete Bitbucket Pull Request Comment",
        "destructiveHint": True,
    },
)
@check_write_access
async def delete_pull_request_comment(
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
                "The comment's current version, from add_pull_request_comment or "
                "get_pull_request_comments. A 409 may mean the version is stale "
                "OR the comment has replies (delete does not cascade)."
            ),
            ge=0,
        ),
    ],
) -> str:
    """Delete a Bitbucket Data Center pull-request comment.

    Deletes the comment via an optimistic-locked delete: 'version' must be the
    comment's current version (from add_pull_request_comment or
    get_pull_request_comments). Delete does not cascade. A 409 may mean the
    version is stale, the comment has replies, or the repository is archived.
    Deleting another user's comment may require elevated (repo-admin)
    permission and otherwise returns 401/409. Blocked when the server runs
    with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        comment_id: The id of the comment to delete.
        version: The comment's current version (optimistic-lock token).

    Returns:
        JSON string confirming the delete (the deleted 'comment_id').

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    await run_bitbucket_fetcher_call(
        bitbucket.delete_comment,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        comment_id=comment_id,
        version=version,
    )
    # A 204 has no body to echo, so confirm with the deleted id.
    return json.dumps({"comment_id": comment_id}, indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Merge Bitbucket Pull Request",
        "destructiveHint": True,
    },
)
@check_write_access
async def merge_pull_request(
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
    version: Annotated[
        int,
        Field(
            description=(
                "The pull request's current version, read from "
                "get_pull_request immediately before merging. Check "
                "get_pull_request_merge_status first. A 409 means a merge "
                "check vetoed, the branches conflict, the pull request is not "
                "open, or the version is stale. Re-read and retry."
            ),
            ge=0,
        ),
    ],
    message: Annotated[
        str | None,
        Field(
            description=(
                "Text placed below the server's generated subject line in "
                "the merge commit. When omitted the commit carries the subject "
                "alone. At most 32768 characters."
            ),
            default=None,
        ),
    ] = None,
    strategy_id: Annotated[
        str | None,
        Field(
            description=(
                "The merge strategy id, passed through to the server, which "
                "accepts only the strategies enabled on the repository. Common "
                "ids: 'no-ff', 'ff', 'ff-only', 'squash', 'squash-ff-only', "
                "'rebase-no-ff', 'rebase-ff-only'. When omitted the "
                "repository default applies."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Merge a pull request now (needs REPO_WRITE).

    Merges the open pull request immediately (it does not queue an auto-merge)
    via an optimistic-locked write: 'version' must be the pull request's
    current version from get_pull_request. Check get_pull_request_merge_status
    first. The tool does not check on its own, so a merge-check veto or a
    conflict is reported as a 409 with the server's reason. The OAuth
    examples configure the REPO_READ scope, so merging needs
    BITBUCKET_OAUTH_SCOPE=REPO_WRITE (or a token issued with it). On a
    timeout or a 5xx the merge may still have been applied: re-read the pull
    request before retrying. Blocked when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        version: The pull request's current version (optimistic-lock token).
        message: Text for the merge commit below the generated subject, or
            None for the subject alone.
        strategy_id: The merge strategy id, or None for the repository default.

    Returns:
        JSON string with the merged pull request (state MERGED and the new
        version).

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    pull_request = await run_bitbucket_fetcher_call(
        bitbucket.merge_pull_request,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        version=version,
        message=message,
        strategy_id=strategy_id,
    )
    return json.dumps(pull_request.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Decline Bitbucket Pull Request",
        "destructiveHint": True,
    },
)
@check_write_access
async def decline_pull_request(
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
    version: Annotated[
        int,
        Field(
            description=(
                "The pull request's current version, read from "
                "get_pull_request immediately before declining. A 409 means "
                "the pull request is not open or changed since you read it. "
                "Re-read and retry."
            ),
            ge=0,
        ),
    ],
    comment: Annotated[
        str | None,
        Field(
            description=(
                "An optional comment explaining why it is declined. At most "
                "32768 characters."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Decline an open Bitbucket Data Center pull request (needs REPO_READ).

    Declines the pull request via an optimistic-locked write: 'version' must
    be the pull request's current version from get_pull_request. A declined
    pull request can be reopened with reopen_pull_request. On a timeout or a
    5xx the decline may still have been applied: re-read the pull request
    before retrying. Blocked when the server runs with READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        version: The pull request's current version (optimistic-lock token).
        comment: An optional comment explaining the decline.

    Returns:
        JSON string with the declined pull request (state DECLINED and the new
        version).

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    pull_request = await run_bitbucket_fetcher_call(
        bitbucket.decline_pull_request,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        version=version,
        comment=comment,
    )
    return json.dumps(pull_request.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={
        "title": "Reopen Bitbucket Pull Request",
        "destructiveHint": False,
    },
)
@check_write_access
async def reopen_pull_request(
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
    version: Annotated[
        int,
        Field(
            description=(
                "The pull request's current version, read from "
                "get_pull_request immediately before reopening. A 409 means "
                "the pull request is not declined (merged ones cannot be "
                "reopened) or changed since you read it. Re-read and retry."
            ),
            ge=0,
        ),
    ],
) -> str:
    """Reopen a declined Bitbucket Data Center pull request (needs REPO_READ).

    Reopens the pull request via an optimistic-locked write: 'version' must be
    the pull request's current version from get_pull_request. A merged pull
    request cannot be reopened. Blocked when the server runs with
    READ_ONLY_MODE.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        repository_slug: The repository slug.
        pull_request_id: The pull-request id.
        version: The pull request's current version (optimistic-lock token).

    Returns:
        JSON string with the reopened pull request (state OPEN and the new
        version).

    Raises:
        ValueError: If in read-only mode.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    pull_request = await run_bitbucket_fetcher_call(
        bitbucket.reopen_pull_request,
        project_key=project_key,
        repository_slug=repository_slug,
        pull_request_id=pull_request_id,
        version=version,
    )
    return json.dumps(pull_request.to_simplified_dict(), indent=2, ensure_ascii=False)


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_users"},
    annotations={"title": "Get Bitbucket Current User", "readOnlyHint": True},
)
async def get_current_user(
    ctx: Context,
    refresh: Annotated[
        bool,
        Field(
            description=(
                "Resolve the caller's identity again instead of reusing the "
                "value cached on this client. Only needed on a long-lived "
                "client after the account has been renamed."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """Get the Bitbucket Data Center user the server is acting as.

    Use this before an author-sensitive step (self-approval rules, "my" pull
    requests) to learn the caller's username and slug. Bitbucket DC has no
    self/whoami endpoint, so resolving the caller's slug costs a priming
    request plus a bounded user-directory lookup before the profile read.
    The resolution is cached per client. A stateless HTTP transport builds a
    client per request, so every call pays it. A long-lived stdio client pays
    it once.

    Args:
        ctx: The FastMCP context.
        refresh: Whether to discard the cached identity and resolve it again.

    Returns:
        JSON string with the caller's profile: id, username (``name``),
        ``slug``, display name, active flag, and account type (``NORMAL`` or
        ``SERVICE``). Contact details are not included.
    """
    bitbucket = await get_bitbucket_fetcher(ctx)
    profile = await run_bitbucket_fetcher_call(
        bitbucket.get_current_user_profile, refresh=refresh
    )
    return json.dumps(profile.to_simplified_dict(), indent=2, ensure_ascii=False)
