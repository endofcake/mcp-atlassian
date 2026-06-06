"""Base client module for Bitbucket Data Center API interactions."""

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from requests import HTTPError, Session
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, SSLError, Timeout

from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.utils.logging import log_config_param
from mcp_atlassian.utils.oauth import configure_oauth_session
from mcp_atlassian.utils.ssl import configure_ssl_verification

from .config import BitbucketConfig

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Bitbucket Data Center core REST API base path.
API_BASE_PATH = "/rest/api/1.0"

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
    """

    values: list[dict[str, Any]]
    is_last_page: bool
    truncated: bool


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
        projects: The collected project objects (at most ``MAX_PROJECTS_LIMIT``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``projects`` omits projects that exist.
    """

    projects: list[dict[str, Any]]
    is_last_page: bool
    truncated: bool


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

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Issue a GET against the Bitbucket DC core REST API.

        Args:
            path: API path relative to ``/rest/api/1.0`` (e.g. ``"/projects"``).
            params: Optional query parameters.

        Returns:
            The parsed JSON response.

        Raises:
            MCPAtlassianAuthenticationError: If the bearer token is rejected
                (HTTP 401/403).
            ValueError: If the request fails to connect, the TLS handshake
                fails, the request times out, returns a non-auth error status,
                or returns a non-JSON body (e.g. an HTML proxy login page on a
                200). The crafted message never embeds the raw transport error,
                so internal host/pool details are not leaked to the client.
        """
        url = f"{self._api_root}{path}"
        try:
            response = self._session.get(
                url, params=params, timeout=self.config.timeout
            )
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
        params: dict[str, Any] | None = None,
        transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    ) -> BitbucketPage:
        """Walk a Bitbucket DC paged endpoint and collect up to ``limit`` items.

        Bitbucket DC paginates with ``start``/``limit`` query params and answers
        with a ``{values, isLastPage, nextPageStart, ...}`` envelope. This
        requests pages of ``page_size`` from ``start=0``, following
        ``nextPageStart`` until the upstream list ends (``isLastPage``), the
        collected count reaches ``limit``, or the ``max_pages`` safety cap is hit.
        ``transform`` is applied to each page's ``values`` before they are
        collected (e.g. a client-side allowlist filter), so it is bounded by the
        same page cap as the raw walk.

        Args:
            path: API path relative to ``/rest/api/1.0`` (e.g. ``"/projects"``).
            limit: Stop once this many items are collected; the result is sliced
                to it. Callers clamp this to their own domain ceiling first.
            page_size: Per-request page size sent as the ``limit`` query param.
            max_pages: Hard bound on the number of page requests.
            params: Extra query params merged into every page request.
            transform: Optional per-page mapping of the ``values`` list.

        Returns:
            A :class:`BitbucketPage` with the collected items (at most ``limit``),
            whether the upstream list was fully consumed (``is_last_page``), and
            whether items were omitted because a bound was hit (``truncated``).

        Raises:
            ValueError: If a page is not a paged object with a ``values`` list, or
                if the request fails (see :meth:`_get`).
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        collected: list[dict[str, Any]] = []
        start = 0
        is_last_page = True
        for _ in range(max_pages):
            page_params = {**(params or {}), "start": start, "limit": page_size}
            page = self._get(path, params=page_params)
            values = self._page_values(page, path)
            if transform is not None:
                values = transform(values)
            collected.extend(values)
            is_last_page = bool(page.get("isLastPage", True))
            next_start = page.get("nextPageStart")
            if is_last_page or next_start is None or len(collected) >= limit:
                break
            start = next_start

        # truncated if we trimmed an overshooting page, or stopped (limit/cap
        # reached) while the upstream list still had more pages.
        truncated = len(collected) > limit or not is_last_page
        return BitbucketPage(
            values=collected[:limit],
            is_last_page=is_last_page,
            truncated=truncated,
        )
