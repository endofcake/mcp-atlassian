"""Base client module for Bitbucket Data Center API interactions."""

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from requests import HTTPError, Session
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, SSLError, Timeout

from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketProject
from mcp_atlassian.utils.logging import log_config_param
from mcp_atlassian.utils.oauth import configure_oauth_session
from mcp_atlassian.utils.ssl import configure_ssl_verification

from .config import BitbucketConfig

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Bitbucket Data Center core REST API base path.
API_BASE_PATH = "/rest/api/1.0"

# Max characters of upstream error text surfaced from a 400/409 write
# rejection. The instance's own ``errors[].message`` is actionable, but it must
# be bounded so a pathological upstream body cannot bloat the client-facing
# error message.
_ERROR_MESSAGE_MAX_CHARS = 500

# Page size and page cap for the user-slug lookup that resolves the caller's
# X-AUSERNAME to a slug. The ``filter`` query is a substring match, so the exact
# name match can fall on a later page in a large directory; the cap bounds the
# scan so a pathological filter cannot walk the whole user base.
_USERS_PAGE_SIZE = 100
_MAX_USER_LOOKUP_PAGES = 10


class BitbucketResourceNotFoundError(ValueError):
    """A Bitbucket resource (project/repository/pull request) was not found.

    Raised by :meth:`BitbucketClient._get` on an HTTP 404 so callers can tell a
    missing/no-access resource apart from a generic network or API failure and
    surface an actionable message. Subclasses :class:`ValueError` so existing
    ``except ValueError`` handlers still catch it (degrading to a network-error
    classification) when a caller does not handle it specifically.
    """


# Default number of projects a single list_projects call returns.
DEFAULT_PROJECTS_LIMIT = 25
# Hard ceiling on projects returned in one call. Bitbucket DC paginates, so an
# unbounded walk could fetch thousands of pages; this caps the result and the
# tool's `limit` parameter, with truncation surfaced explicitly to the caller.
MAX_PROJECTS_LIMIT = 1000
# Per-request page size for the underlying paged endpoint.
_PROJECTS_PAGE_SIZE = 100
# Bound on the pagination loop. Without a filter, reaching MAX_PROJECTS_LIMIT
# needs ceil(1000 / 100) = 10 full pages and the extra headroom just tolerates
# short pages. With a projects_filter it is the real search-depth ceiling: each
# page is narrowed before the limit bound, so an unmatched filter scans up to
# 50 * 100 = 5000 upstream projects before giving up (truncated, not last page).
# It also bounds calls to a misbehaving instance.
_MAX_PROJECT_PAGES = 50


@dataclass
class BitbucketPage:
    """A bounded, possibly-truncated page of items from a paged DC endpoint.

    The domain-agnostic result of :meth:`BitbucketClient._paginate`. See
    :class:`BitbucketProjectsPage` for the full meaning of the ``is_last_page`` /
    ``truncated`` combinations; the semantics are identical, only the field name
    of the collected items differs.

    Attributes:
        values: The collected raw item dicts (at most the requested ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``values`` omits items that exist.
        next_page_start: The ``start`` offset to resume from after this page, or
            ``None`` when there is no forward cursor — i.e. the list was fully
            consumed (``is_last_page`` True), the window overshot ``limit`` (the
            trimmed tail is unreachable forward; raise ``limit``), or a non-last
            page advertised no cursor (incomplete, not resumable). Read it with
            ``truncated`` / ``is_last_page`` to tell "done" from "incomplete".
    """

    values: list[dict[str, Any]]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


@dataclass
class BitbucketProjectsPage:
    """A bounded, possibly-truncated view of Bitbucket projects.

    ``truncated`` is the authoritative completeness signal: the result holds
    every visible project iff ``truncated`` is False. ``is_last_page`` reports a
    finer detail — whether the scan reached the end of the upstream list — which
    tells the caller *why* a truncated result is incomplete. The meaningful
    combinations are:

    - ``is_last_page=True,  truncated=False`` — complete; nothing was omitted.
    - ``is_last_page=True,  truncated=True``  — the scan reached the end, but more
      projects matched than ``limit``; raise ``limit`` to get the rest (there are
      no further pages to fetch).
    - ``is_last_page=False, truncated=True``  — the scan stopped before the end
      (``limit`` or the page cap was reached); more projects may exist beyond
      what was examined. With a ``projects_filter`` and an empty result this
      means "scan incomplete", not "definitively none".

    ``is_last_page=False, truncated=False`` cannot occur.

    Attributes:
        projects: The collected project models (at most ``MAX_PROJECTS_LIMIT``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``projects`` omits projects that exist.
        next_page_start: The ``start`` cursor to resume from, or ``None`` when
            nothing more is fetchable (see :class:`BitbucketPage`).
    """

    projects: list[BitbucketProject]
    is_last_page: bool
    truncated: bool
    next_page_start: int | None = None


class BitbucketClient:
    """Bitbucket Data Center REST base client.

    Holds a ``requests.Session`` that carries the user's OAuth 2.0 ``Bearer``
    token and issues read calls against the Data Center core REST API
    (``/rest/api/1.0``). The bearer is supplied per request, so one client is
    built per authenticated user. Domain operations live in mixins
    (:class:`~mcp_atlassian.bitbucket.projects.ProjectsMixin`,
    :class:`~mcp_atlassian.bitbucket.repositories.ReposMixin`) composed into
    ``BitbucketFetcher``; this base provides the session, the ``_get`` error
    taxonomy, and the shared pagination helper.
    """

    config: BitbucketConfig

    def __init__(self, config: BitbucketConfig | None = None) -> None:
        """Initialize the Bitbucket client.

        Args:
            config: Optional configuration object (uses env vars if not provided).

        Raises:
            ValueError: If the configuration is invalid or OAuth is missing.
            MCPAtlassianAuthenticationError: If OAuth session setup fails.
        """
        self.config = config or BitbucketConfig.from_env()

        # Per-user identity cache for write paths that need the caller's slug
        # (review status). X-AUSERNAME is captured opportunistically from
        # authenticated responses; the resolved slug is memoised — the client
        # is built per authenticated user, so the cache is per-user.
        self._auth_username: str | None = None
        self._current_user_slug: str | None = None

        # auth_type is a Literal["oauth"], so OAuth is the only valid value;
        # the only thing that can be missing is the oauth_config itself.
        if not self.config.oauth_config:
            error_msg = "OAuth authentication requires oauth_config"
            raise ValueError(error_msg)

        self._session = Session()
        if not configure_oauth_session(self._session, self.config.oauth_config):
            error_msg = "Failed to configure Bitbucket OAuth session"
            raise MCPAtlassianAuthenticationError(error_msg)

        # Prevent .netrc from overriding the explicit Bearer credential (#860).
        self._session.trust_env = False

        configure_ssl_verification(
            service_name="Bitbucket",
            url=self.config.url,
            session=self._session,
            ssl_verify=self.config.ssl_verify,
        )

        proxies: dict[str, str] = {}
        if self.config.http_proxy:
            proxies["http"] = self.config.http_proxy
        if self.config.https_proxy:
            proxies["https"] = self.config.https_proxy
        if self.config.socks_proxy:
            proxies["socks"] = self.config.socks_proxy
        if proxies:
            self._session.proxies.update(proxies)
            for key, value in proxies.items():
                log_config_param(
                    logger, "Bitbucket", f"{key.upper()}_PROXY", value, sensitive=True
                )
        if self.config.no_proxy and isinstance(self.config.no_proxy, str):
            os.environ["NO_PROXY"] = self.config.no_proxy
            log_config_param(logger, "Bitbucket", "NO_PROXY", self.config.no_proxy)

        if self.config.custom_headers:
            self._session.headers.update(self.config.custom_headers)

    @property
    def _api_root(self) -> str:
        """Return the Bitbucket DC core REST API root URL.

        Returns:
            The base URL joined with the ``/rest/api/1.0`` path.
        """
        return f"{self.config.url.rstrip('/')}{API_BASE_PATH}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        """Issue a request against the Bitbucket DC core REST API.

        Carries the leak-free error taxonomy shared by every verb:
        :meth:`_get`/:meth:`_post`/:meth:`_put` are thin wrappers. Connection,
        TLS, timeout, auth (401/403), not-found (404), client/conflict
        (400/409), non-JSON-body, and any other transport failure map to a
        crafted message that never embeds the raw transport error — so internal
        host/pool details are not leaked to the client. The sole exception is a
        400/409, where the instance's own ``errors[].message`` text (and nothing
        else from the body) is appended so a write rejection is actionable.

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, ``"PUT"``).
            path: API path relative to ``/rest/api/1.0`` (e.g. ``"/projects"``).
            params: Optional query parameters.
            json_body: Optional JSON request body (writes only). When None no
                body is attached, so a GET issues the exact original call.

        Returns:
            The parsed JSON response.

        Raises:
            MCPAtlassianAuthenticationError: If the bearer token is rejected
                (HTTP 401/403).
            BitbucketResourceNotFoundError: On HTTP 404 (a ValueError subclass).
            ValueError: If the request fails to connect, the TLS handshake
                fails, the request times out, returns another error status, or
                returns a non-JSON body (e.g. an HTML proxy login page on a
                200). The crafted message never embeds the raw transport error,
                so internal host/pool details are not leaked to the client.
        """
        url = f"{self._api_root}{path}"
        # A GET must call the session with the exact original kwargs (no
        # ``json=``) so existing behaviour — and the tests that pin it — stay
        # byte-identical; only a write attaches a body.
        request_kwargs: dict[str, Any] = {
            "params": params,
            "timeout": self.config.timeout,
        }
        if json_body is not None:
            request_kwargs["json"] = json_body
        http_method: Callable[..., Any] = getattr(self._session, method.lower())
        try:
            response = http_method(url, **request_kwargs)
            self._capture_auth_username(response)
            response.raise_for_status()
            return response.json()
        except SSLError as e:
            # SSLError subclasses ConnectionError, so this clause must precede
            # the ConnectionError branch. str(e) carries the urllib3 pool repr
            # (host/port), so it is deliberately not interpolated.
            error_msg = (
                f"SSL verification failed connecting to Bitbucket at "
                f"{self.config.url}. Check the instance certificate, or set "
                "BITBUCKET_SSL_VERIFY=false for a self-signed certificate."
            )
            logger.error(error_msg)
            raise ValueError(error_msg) from e
        except RequestsConnectionError as e:
            error_msg = (
                f"Could not connect to Bitbucket at {self.config.url}. "
                "Check that BITBUCKET_URL is correct and the instance is reachable."
            )
            logger.error(error_msg)
            raise ValueError(error_msg) from e
        except Timeout as e:
            # ReadTimeout descends from Timeout, not ConnectionError, so it
            # escaped the branch above and leaked the urllib3 pool string. str(e)
            # carries that host/pool repr, so it is deliberately not interpolated.
            error_msg = (
                f"Request to Bitbucket at {self.config.url} timed out. The "
                "instance may be slow or unreachable; check connectivity or "
                "raise BITBUCKET_TIMEOUT."
            )
            logger.error(error_msg)
            raise ValueError(error_msg) from e
        except HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403):
                error_msg = (
                    "Bitbucket authentication failed (HTTP "
                    f"{status}). The forwarded OAuth bearer token was rejected "
                    "or lacks permission for this resource."
                )
                logger.error(error_msg)
                raise MCPAtlassianAuthenticationError(error_msg) from e
            if status == 404:
                # The path is caller-derived and already percent-encoded; the
                # upstream body is never echoed, so this stays leak-free.
                error_msg = (
                    f"Bitbucket resource not found (HTTP 404) for {path}. The "
                    "project, repository, or pull request does not exist, or the "
                    "authenticated user lacks permission to view it."
                )
                logger.error(error_msg)
                raise BitbucketResourceNotFoundError(error_msg) from e
            if status in (400, 409):
                # A write rejection (bad anchor, author-self-approve, stale
                # version). Surface only the instance's own errors[].message —
                # nothing else from the body — so the caller can act on it.
                detail = self._extract_error_messages(e.response)
                error_msg = (
                    f"Bitbucket API request to {path} failed with HTTP {status}"
                    + (f": {detail}" if detail else ".")
                )
                logger.error(error_msg)
                raise ValueError(error_msg) from e
            error_msg = (
                f"Bitbucket API request to {path} failed with HTTP "
                f"{status if status is not None else 'unknown'}."
            )
            logger.error(error_msg)
            raise ValueError(error_msg) from e
        except json.JSONDecodeError as e:
            # A 2xx with a non-JSON body usually means an intermediary (proxy
            # login page, SSO redirect) answered instead of Bitbucket.
            error_msg = (
                f"Bitbucket returned a non-JSON response for {path}. This often "
                "indicates an authentication proxy or login page intercepted the "
                "request rather than the Bitbucket API."
            )
            logger.error(error_msg)
            raise ValueError(error_msg) from e
        except RequestException as e:
            # Backstop for any other transport-layer failure (chunked-encoding,
            # content-decoding, redirect loops, ...). Must stay last: HTTPError
            # and requests' JSONDecodeError also subclass RequestException and
            # are handled above. str(e) can carry the urllib3 pool repr, so it is
            # logged server-side only and never surfaced to the client.
            logger.error("Bitbucket request to %s failed: %s", path, e)
            error_msg = f"Bitbucket API request to {path} failed (network error)."
            raise ValueError(error_msg) from e

    @staticmethod
    def _extract_error_messages(response: Any) -> str | None:
        """Extract the joined ``errors[].message`` text from an error body.

        Bitbucket DC reports request errors with a documented envelope —
        ``{"errors": [{"message": ...}, ...]}``. Only the human-readable
        ``message`` strings are surfaced (never ``exceptionName``, ``context``,
        the raw body, or transport internals), joined and length-capped, so a
        400/409 is actionable without leaking instance internals.

        Args:
            response: The error response object.

        Returns:
            The joined, capped messages, or None if the body is not a JSON
            object of the documented shape — in which case the caller falls back
            to a generic status-only message.
        """
        try:
            body = response.json()
        except (ValueError, TypeError):
            return None
        if not isinstance(body, dict):
            return None
        errors = body.get("errors")
        if not isinstance(errors, list):
            return None
        messages = [
            item["message"].strip()
            for item in errors
            if isinstance(item, dict)
            and isinstance(item.get("message"), str)
            and item["message"].strip()
        ]
        if not messages:
            return None
        return "; ".join(messages)[:_ERROR_MESSAGE_MAX_CHARS]

    def _capture_auth_username(self, response: Any) -> None:
        """Record the authenticated username from the ``X-AUSERNAME`` header.

        Bitbucket DC sets ``X-AUSERNAME`` on authenticated REST responses. It is
        the only way this REST surface exposes the caller's identity (there is
        no self/whoami endpoint), so it is captured opportunistically on every
        response and later resolved to a user slug. It is *not* part of the
        OpenAPI spec — documented-in-practice — so it is read defensively and an
        absent or non-string header simply leaves the cache untouched.
        """
        username = response.headers.get("X-AUSERNAME")
        if isinstance(username, str) and username:
            self._auth_username = username

    @staticmethod
    def _repo_base_path(project_key: str, repository_slug: str) -> str:
        """Build and validate the repo-scoped REST path `/projects/{key}/repos/{slug}`.

        Both caller-supplied segments are percent-encoded with quote(safe='') (no
        unescaped slashes) to prevent path traversal.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).

        Returns:
            ``/projects/{key}/repos/{slug}`` with both caller segments
            percent-encoded.

        Raises:
            ValueError: If either caller segment is blank.
        """
        key = project_key.strip()
        slug = repository_slug.strip()
        if not key:
            raise ValueError("project_key must be a non-empty Bitbucket project key.")
        if not slug:
            raise ValueError(
                "repository_slug must be a non-empty Bitbucket repository slug."
            )
        return f"/projects/{quote(key, safe='')}/repos/{quote(slug, safe='')}"

    @staticmethod
    def _coerce_pr_id(pull_request_id: int | str) -> int:
        """Coerce and validate a pull-request id to a positive integer.

        Shared by the pull-request and commit mixins (both build
        ``.../pull-requests/{id}/...`` paths).

        Args:
            pull_request_id: The caller-supplied pull-request id.

        Returns:
            The id as a positive ``int`` (safe to interpolate into the path —
            an integer cannot carry traversal or injection).

        Raises:
            ValueError: If the id is not a positive integer.
        """
        try:
            pr_id = int(pull_request_id)
        except (TypeError, ValueError):
            raise ValueError("pull_request_id must be a positive integer.") from None
        if pr_id <= 0:
            raise ValueError("pull_request_id must be a positive integer.")
        return pr_id

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Issue a GET against the Bitbucket DC core REST API.

        Thin wrapper over :meth:`_request`; see it for the shared error
        taxonomy.

        Args:
            path: API path relative to ``/rest/api/1.0`` (e.g. ``"/projects"``).
            params: Optional query parameters.

        Returns:
            The parsed JSON response.
        """
        return self._request("GET", path, params=params)

    def _post(self, path: str, *, json_body: Any) -> Any:
        """Issue a POST against the Bitbucket DC core REST API.

        Thin wrapper over :meth:`_request`; see it for the shared error
        taxonomy. On a 400/409 the raised ValueError carries the instance's own
        ``errors[].message`` text, so a write rejection is actionable.

        Args:
            path: API path relative to ``/rest/api/1.0``.
            json_body: The JSON request body.

        Returns:
            The parsed JSON response.
        """
        return self._request("POST", path, json_body=json_body)

    def _put(self, path: str, *, json_body: Any) -> Any:
        """Issue a PUT against the Bitbucket DC core REST API.

        Thin wrapper over :meth:`_request`; see it for the shared error
        taxonomy. On a 400/409 the raised ValueError carries the instance's own
        ``errors[].message`` text, so a write rejection is actionable.

        Args:
            path: API path relative to ``/rest/api/1.0``.
            json_body: The JSON request body.

        Returns:
            The parsed JSON response.
        """
        return self._request("PUT", path, json_body=json_body)

    def get_current_user(self) -> dict[str, Any]:
        """Validate the session by querying the inbox pull-request count.

        Bitbucket DC has no plain ``myself`` endpoint; the authenticated
        ``/inbox/pull-requests/count`` endpoint is a cheap, read-only call that
        only succeeds with a valid bearer token, so it doubles as a token check.

        Returns:
            The parsed JSON response from the validation endpoint.

        Raises:
            MCPAtlassianAuthenticationError: If validation fails.
        """
        result = self._get("/inbox/pull-requests/count")
        if not isinstance(result, dict):
            raise MCPAtlassianAuthenticationError(
                f"Unexpected Bitbucket validation response: {result!r}"
            )
        return result

    def _resolve_current_user_slug(self) -> str:
        """Resolve the authenticated caller's user slug, memoised per client.

        Bitbucket DC's REST surface has no self/whoami endpoint, so the caller's
        identity is learned in two steps: the username comes from the
        ``X-AUSERNAME`` header (primed here with a cheap authenticated call if no
        prior response has set it), then the *slug* the participant path needs is
        resolved from ``GET /users?filter=<username>`` by exact-matching the
        ``name`` field. The slug is cached for the life of this (per-user) client.

        Returns:
            The authenticated user's slug.

        Raises:
            ValueError: If the instance does not expose ``X-AUSERNAME`` (the
                caller identity is unknown) or no user exactly matches it — the
                caller's slug is never guessed.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        if self._current_user_slug is not None:
            return self._current_user_slug

        # Prime X-AUSERNAME if no prior response has set it: any authenticated
        # response carries it, and the inbox count is the cheapest such call.
        if self._auth_username is None:
            self._get("/inbox/pull-requests/count")

        username = self._auth_username
        if not username:
            raise ValueError(
                "Could not determine the authenticated user: the Bitbucket "
                "instance did not return an X-AUSERNAME header, so review status "
                "cannot be set without the caller's identity."
            )

        # The filter is a substring match, so several users can come back and the
        # exact match may fall on a later page; walk pages until it is found or
        # the list is exhausted, taking only the exact name match (never a
        # near-match). The page cap bounds a pathological filter.
        start = 0
        for _ in range(_MAX_USER_LOOKUP_PAGES):
            result = self._get(
                "/users",
                params={
                    "filter": username,
                    "start": start,
                    "limit": _USERS_PAGE_SIZE,
                },
            )
            for user in self._page_values(result, "/users"):
                if user.get("name") == username:
                    slug = user.get("slug")
                    if isinstance(slug, str) and slug:
                        self._current_user_slug = slug
                        return slug
            if bool(result.get("isLastPage", True)):
                break
            next_start = result.get("nextPageStart")
            if next_start is None:
                break
            start = next_start

        raise ValueError(
            f"Could not resolve a user slug for the authenticated user "
            f"'{username}'; the users endpoint returned no exact name match."
        )

    @staticmethod
    def _page_values(page: Any, path: str) -> list[dict[str, Any]]:
        """Extract and validate the ``values`` list from a paged response.

        Args:
            page: The parsed JSON body of a Bitbucket paged endpoint.
            path: The API path, for the error message.

        Returns:
            The ``values`` list.

        Raises:
            ValueError: If the body is not a paged object carrying a ``values``
                list of objects. A missing/misshaped body must surface as an
                error rather than masquerade as an empty (successful) result, and
                must do so identically whether or not a per-page transform is
                applied (a non-dict entry would otherwise crash the transform).
        """
        if not isinstance(page, dict) or not isinstance(page.get("values"), list):
            raise ValueError(
                f"Bitbucket returned an unexpected response shape for {path}; "
                "expected a paged object with a 'values' list."
            )
        values = page["values"]
        if not all(isinstance(item, dict) for item in values):
            raise ValueError(
                f"Bitbucket returned an unexpected response shape for {path}; "
                "expected every 'values' entry to be an object."
            )
        return values

    def _paginate(
        self,
        path: str,
        *,
        limit: int,
        page_size: int,
        max_pages: int,
        start: int = 0,
        params: dict[str, Any] | None = None,
        transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    ) -> BitbucketPage:
        """Walk a Bitbucket DC paged endpoint and collect up to ``limit`` items.

        Bitbucket DC paginates with ``start``/``limit`` query params and answers
        with a ``{values, isLastPage, nextPageStart, ...}`` envelope. This
        requests pages of ``page_size`` from ``start``, following
        ``nextPageStart`` until the upstream list ends (``isLastPage``), the
        collected count reaches ``limit``, or the ``max_pages`` safety cap is hit.
        ``transform`` is applied to each page's ``values`` before they are
        collected (e.g. a client-side allowlist filter), so it is bounded by the
        same page cap as the raw walk.

        With ``max_pages=1`` and ``page_size == limit`` this fetches a single
        window and surfaces the upstream cursor directly, so the caller resumes
        exactly where the window ended (no aggregation ambiguity).

        Args:
            path: API path relative to ``/rest/api/1.0`` (e.g. ``"/projects"``).
            limit: Stop once this many items are collected; the result is sliced
                to it. Callers clamp this to their own domain ceiling first.
            page_size: Per-request page size sent as the ``limit`` query param.
            max_pages: Hard bound on the number of page requests.
            start: The offset of the first page to fetch (a resume cursor). 0
                starts from the beginning.
            params: Extra query params merged into every page request.
            transform: Optional per-page mapping of the ``values`` list.

        Returns:
            A :class:`BitbucketPage` with the collected items (at most ``limit``),
            whether the upstream list was fully consumed (``is_last_page``),
            whether items were omitted because a bound was hit (``truncated``),
            and ``next_page_start`` — the cursor to resume from, or ``None`` when
            there is no forward cursor. ``None`` arises three ways: the upstream
            list ended (``is_last_page`` True, ``truncated`` False — complete); the
            window overshot ``limit`` (``truncated`` True — the trimmed tail lives
            inside an already-returned page, unreachable by a forward cursor, so
            the caller raises ``limit``); or a non-last page advertised no cursor
            (``truncated`` True — incomplete, not resumable). A non-null
            ``next_page_start`` is always the exact upstream offset to resume from.

        Raises:
            ValueError: If a page is not a paged object with a ``values`` list, or
                if the request fails (see :meth:`_get`).
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        collected: list[dict[str, Any]] = []
        next_start = start  # cursor for the page about to be fetched
        page_start = start  # start offset of the most-recently-fetched page
        is_last_page = True
        last_next_start = None  # nextPageStart reported by the most recent page
        for _ in range(max_pages):
            page_start = next_start
            page_params = {**(params or {}), "start": page_start, "limit": page_size}
            page = self._get(path, params=page_params)
            values = self._page_values(page, path)
            if transform is not None:
                values = transform(values)
            collected.extend(values)
            is_last_page = bool(page.get("isLastPage", True))
            last_next_start = page.get("nextPageStart")
            if is_last_page or last_next_start is None or len(collected) >= limit:
                break
            next_start = last_next_start

        overshot = len(collected) > limit
        # truncated if we trimmed an overshooting page, or stopped (limit/cap
        # reached) while the upstream list still had more pages.
        truncated = overshot or not is_last_page
        next_page_start: int | None
        if overshot:
            # A window denser than ``limit``: the trimmed tail lives inside an
            # already-returned page, so a forward cursor cannot recover it without
            # re-yielding this window. Advertise no cursor (``truncated`` is true);
            # the caller raises ``limit`` to get the rest. Only the
            # client-side-filtered walk can overshoot — the single-window paths
            # request ``page_size == limit``, so a page never exceeds ``limit``.
            next_page_start = None
        elif is_last_page:
            next_page_start = None
        else:
            # A full window with more upstream pages: resume from the advertised
            # cursor, or None when the page gave none ("incomplete, can't resume").
            next_page_start = last_next_start
        return BitbucketPage(
            values=collected[:limit],
            is_last_page=is_last_page,
            truncated=truncated,
            next_page_start=next_page_start,
        )
