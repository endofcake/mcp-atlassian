"""Bitbucket Data Center ref-comparison operations."""

from typing import Any

from ..models.bitbucket import (
    BitbucketChange,
    BitbucketCommit,
    BitbucketPullRequestDiff,
)
from ..utils.pagination import clamp_limit
from .client import (
    BitbucketClient,
    BitbucketResourceNotFoundError,
    BitbucketResponseTooLargeError,
)
from .commits import DEFAULT_COMMITS_LIMIT, MAX_COMMITS_LIMIT, BitbucketCommitsPage
from .pull_requests import (
    DEFAULT_CHANGES_LIMIT,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_LINES_PER_FILE,
    MAX_CHANGES_LIMIT,
    MAX_CONTEXT_LINES,
    MAX_MAX_FILES,
    MAX_MAX_LINES_PER_FILE,
    BitbucketChangesPage,
)

# The one value the compare ``whitespace`` query param accepts. It is
# normalized through BitbucketClient._enum_param (a blank value is dropped,
# any other value raises).
_WHITESPACE_VALUES = ("ignore-all",)


class CompareMixin(BitbucketClient):
    """Mixin for comparing two refs or commits of a Bitbucket Data Center repository.

    The three ``compare`` endpoints share a query contract: ``from`` is the
    source commit (the side whose changes are reported), ``to`` is the target
    commit, and either may be a partial or full commit id or a qualified or
    unqualified ref name. When ``to`` is omitted the server substitutes the
    repository's default branch. ``fromRepo`` names another repository (a
    fork) that holds the source commit.
    """

    @staticmethod
    def _compare_params(
        from_ref: str, to_ref: str | None, from_repo: str | None
    ) -> dict[str, Any]:
        """Build the query params shared by the compare endpoints.

        ``from_ref`` is required because the server substitutes the default
        branch for an omitted ``from``, which with an omitted ``to`` compares
        the default branch against itself and always returns nothing.
        ``to_ref`` is optional and, when blank, is not sent so the server
        default (the repository's default branch) applies. Both are query
        values. The HTTP layer percent-encodes them, so they are only
        stripped here.

        ``from_repo`` is accepted in ``PROJECT/slug`` form only. Each half is
        validated with the path-segment rules (non-blank, not ``.`` or ``..``)
        so that a traversal-shaped value is rejected before any request; the
        value travels as a query parameter, so it is sent unencoded.

        Args:
            from_ref: The source commit or ref name.
            to_ref: The target commit or ref name, or None/blank for the
                repository's default branch.
            from_repo: Optional ``PROJECT/slug`` of the repository holding the
                source commit when it is not in the current repository.

        Returns:
            A params dict carrying ``from`` and, when supplied, ``to`` and
            ``fromRepo``.

        Raises:
            ValueError: If ``from_ref`` is blank, or ``from_repo`` is not a
                single ``PROJECT/slug`` pair of valid segments.
        """
        source = from_ref.strip() if from_ref else ""
        if not source:
            raise ValueError("from_ref must be a non-empty commit id or ref name.")
        params: dict[str, Any] = {"from": source}
        target = to_ref.strip() if to_ref else ""
        if target:
            params["to"] = target
        repo_text = from_repo.strip() if from_repo else ""
        if repo_text:
            parts = repo_text.split("/")
            if len(parts) != 2:
                raise ValueError(
                    "from_repo must be a project key and repository slug "
                    "separated by one slash (PROJECT/slug)."
                )
            key, slug = parts
            BitbucketClient._encode_segment(key, name="from_repo", what="project key")
            BitbucketClient._encode_segment(
                slug, name="from_repo", what="repository slug"
            )
            params["fromRepo"] = f"{key.strip()}/{slug.strip()}"
        return params

    @staticmethod
    def _not_found(url: str, path: str | None = None) -> BitbucketResourceNotFoundError:
        """Build the 404 error for a compare request, naming the likely causes.

        The client's generic 404 text names the project, repository, and pull
        request. On the compare endpoints the missing piece is more often a
        ref or the fork named by ``from_repo``, so the message says so. With
        ``path`` (the single-file diff form) it also names the file.

        Args:
            url: The request path, already percent-encoded.
            path: The caller's file path for the single-file diff form.

        Returns:
            The error to raise. The caller chains it from the original.
        """
        detail = (
            f"Bitbucket resource not found (HTTP 404) for {url}. The repository, "
            "from_ref, to_ref, or the repository named by from_repo does not "
            "exist, or the authenticated user lacks permission to view it"
        )
        if path is None:
            return BitbucketResourceNotFoundError(f"{detail}.")
        return BitbucketResourceNotFoundError(
            f"{detail}, or the comparison does not change the file '{path}' "
            "(check the changed-file listing, and set src_path for a moved file)."
        )

    def compare_changes(
        self,
        project_key: str,
        repository_slug: str,
        from_ref: str,
        *,
        to_ref: str | None = None,
        from_repo: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_CHANGES_LIMIT,
    ) -> BitbucketChangesPage:
        """List the files changed between two refs of a repository.

        Calls ``GET .../compare/changes`` (paged with ``start``/``limit``).
        Each call fetches one window, resumable via the returned
        ``next_page_start``.
        The listing reports the changes present in ``from_ref`` but not in
        ``to_ref``, and is the discovery step for a per-file diff: pass an
        entry's ``path`` (and ``src_path`` for a move or copy) to
        :meth:`compare_diff`.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            from_ref: The source commit or ref name.
            to_ref: The target commit or ref name, or None for the
                repository's default branch.
            from_repo: Optional ``PROJECT/slug`` of the repository holding the
                source commit (see :meth:`_compare_params`).
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of changed files to return. Clamped to
                ``[1, MAX_CHANGES_LIMIT]``.

        Returns:
            A :class:`~mcp_atlassian.bitbucket.pull_requests.BitbucketChangesPage`
            carrying the collected changes, whether the upstream list was fully
            consumed (``is_last_page``), whether files were omitted
            (``truncated``), and the ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, ``from_ref`` is blank,
                ``from_repo`` is malformed, a page is misshaped, or the request
                fails.
            BitbucketResourceNotFoundError: If the repository, a ref, or the
                ``from_repo`` repository does not exist or is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._repo_base_path(project_key, repository_slug)
        params = self._compare_params(from_ref, to_ref, from_repo)
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.compare_changes"),
                MAX_CHANGES_LIMIT,
            ),
        )
        url = f"{base}/compare/changes"
        try:
            page = self._fetch_page(url, limit=limit, start=start, params=params)
        except BitbucketResourceNotFoundError as e:
            raise self._not_found(url) from e
        changes = [BitbucketChange.from_api_response(value) for value in page.values]
        return BitbucketChangesPage(
            changes=changes,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    def compare_commits(
        self,
        project_key: str,
        repository_slug: str,
        from_ref: str,
        *,
        to_ref: str | None = None,
        from_repo: str | None = None,
        start: int = 0,
        limit: int = DEFAULT_COMMITS_LIMIT,
    ) -> BitbucketCommitsPage:
        """List the commits reachable from one ref but not from another.

        Calls ``GET .../compare/commits`` (paged with ``start``/``limit``).
        Each call fetches one window, resumable via the returned
        ``next_page_start``.
        The listing reports the commits reachable from ``from_ref`` that are
        not reachable from ``to_ref``.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).
            from_ref: The source commit or ref name.
            to_ref: The target commit or ref name, or None for the
                repository's default branch.
            from_repo: Optional ``PROJECT/slug`` of the repository holding the
                source commit (see :meth:`_compare_params`).
            start: The offset to resume from (the ``next_page_start`` of a prior
                call). 0 starts from the beginning.
            limit: Maximum number of commits to return. Clamped to
                ``[1, MAX_COMMITS_LIMIT]``.

        Returns:
            A :class:`~mcp_atlassian.bitbucket.commits.BitbucketCommitsPage`
            carrying the collected commits, whether the upstream list was fully
            consumed (``is_last_page``), whether commits were omitted
            (``truncated``), and the ``next_page_start`` resume cursor.

        Raises:
            ValueError: If a segment is blank, ``from_ref`` is blank,
                ``from_repo`` is malformed, a page is misshaped, or the request
                fails.
            BitbucketResourceNotFoundError: If the repository, a ref, or the
                ``from_repo`` repository does not exist or is not accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._repo_base_path(project_key, repository_slug)
        params = self._compare_params(from_ref, to_ref, from_repo)
        limit = max(
            1,
            min(
                clamp_limit(limit, context="bitbucket.compare_commits"),
                MAX_COMMITS_LIMIT,
            ),
        )
        url = f"{base}/compare/commits"
        try:
            page = self._fetch_page(url, limit=limit, start=start, params=params)
        except BitbucketResourceNotFoundError as e:
            raise self._not_found(url) from e
        commits = [BitbucketCommit.from_api_response(value) for value in page.values]
        return BitbucketCommitsPage(
            commits=commits,
            is_last_page=page.is_last_page,
            truncated=page.truncated,
            next_page_start=page.next_page_start,
        )

    def compare_diff(
        self,
        project_key: str,
        repository_slug: str,
        from_ref: str,
        *,
        to_ref: str | None = None,
        from_repo: str | None = None,
        max_lines_per_file: int = DEFAULT_MAX_LINES_PER_FILE,
        max_files: int = DEFAULT_MAX_FILES,
        context_lines: int | None = None,
        path: str | None = None,
        src_path: str | None = None,
        whitespace: str | None = None,
    ) -> BitbucketPullRequestDiff:
        """Get the structured diff between two refs, bounded for token control.

        Without ``path`` this calls the whole-comparison form ``GET
        .../compare/diff``; with ``path`` it calls ``GET
        .../compare/diff/{path}`` for that one file. Either form is one
        request. Both forms accept the ``diffs`` envelope the pull-request
        endpoint returns and the bare ``RestDiff`` the specification declares
        (see :meth:`BitbucketClient._build_diff`). The endpoint has no
        server-side size parameter, so the download is bounded by the
        client's byte cap. A larger body raises with the narrowing options
        that apply: list the changed files, fetch one file by ``path``, or
        lower ``context_lines``. The per-file line budget and file cap are
        applied when the model is built and reported through the
        ``truncated`` flags.

        Args:
            project_key: The project key.
            repository_slug: The repository slug.
            from_ref: The source commit or ref name.
            to_ref: The target commit or ref name, or None for the
                repository's default branch.
            from_repo: Optional ``PROJECT/slug`` of the repository holding the
                source commit (see :meth:`_compare_params`).
            max_lines_per_file: Per-file diff-line cap. Clamped to
                ``[1, MAX_MAX_LINES_PER_FILE]``.
            max_files: Maximum number of file diffs to retain. Clamped to
                ``[1, MAX_MAX_FILES]``. Ignored when ``path`` is set.
            context_lines: Optional number of context lines around each change,
                sent server-side as ``contextLines``. Clamped to
                ``[0, MAX_CONTEXT_LINES]``; None uses the server default.
            path: Optional file path to diff on its own. Component rules are
                those of :meth:`BitbucketClient._encode_repo_path`.
            src_path: The file's previous path when it was moved, copied, or
                renamed, sent as ``srcPath``. Requires ``path``.
            whitespace: Optional whitespace handling. ``ignore-all`` is the
                only value the endpoint accepts. A blank value is dropped.

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequestDiff`.

        Raises:
            ValueError: If a segment is blank, ``from_ref`` is blank,
                ``from_repo`` is malformed, a path component is invalid,
                ``src_path`` is given without ``path``, ``whitespace`` is not
                a recognised value, the response is not a diff object, or the
                request fails.
            BitbucketResponseTooLargeError: If the body exceeds the byte cap
                (a ValueError subclass). The message names the narrowing
                options that apply to the form that was called.
            BitbucketResourceNotFoundError: If the repository, a ref, the
                ``from_repo`` repository, or the file does not exist or is not
                accessible.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        base = self._repo_base_path(project_key, repository_slug)
        params = self._compare_params(from_ref, to_ref, from_repo)
        max_lines_per_file = max(1, min(max_lines_per_file, MAX_MAX_LINES_PER_FILE))
        max_files = max(1, min(max_files, MAX_MAX_FILES))
        if context_lines is not None:
            params["contextLines"] = max(0, min(context_lines, MAX_CONTEXT_LINES))
        whitespace_mode = self._enum_param(
            whitespace, name="whitespace", allowed=_WHITESPACE_VALUES
        )
        if whitespace_mode is not None:
            params["whitespace"] = whitespace_mode
        encoded_path = self._encode_repo_path(path, what="path")
        # src_path travels as a query value, which the HTTP layer encodes. It
        # is validated with the same component rules but sent unencoded.
        src_path_text = src_path.strip() if src_path else ""
        if src_path_text:
            self._encode_repo_path(src_path_text, what="src_path")
            if not encoded_path:
                raise ValueError("src_path requires path (the file's current path).")
            params["srcPath"] = src_path_text
        url = f"{base}/compare/diff" + (f"/{encoded_path}" if encoded_path else "")
        try:
            data = self._get(url, params=params)
        except BitbucketResourceNotFoundError as e:
            raise self._not_found(url, path if encoded_path else None) from e
        except BitbucketResponseTooLargeError as e:
            if encoded_path:
                options = "Available option: lower context_lines."
            else:
                options = (
                    "Available options: list the changed files with "
                    "bitbucket_compare_changes, fetch one file with "
                    "bitbucket_compare_diff and its path, or lower "
                    "context_lines."
                )
            raise BitbucketResponseTooLargeError(f"{e} {options}") from e
        return self._build_diff(
            data,
            url,
            single_file=bool(encoded_path),
            max_lines_per_file=max_lines_per_file,
            max_files=max_files,
        )
