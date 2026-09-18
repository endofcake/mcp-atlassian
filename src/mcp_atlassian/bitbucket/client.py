"""Base client module for Bitbucket Data Center API interactions."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from requests import HTTPError, Session
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, SSLError, Timeout

from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import (
    BitbucketFileDiff,
    BitbucketProject,
    BitbucketPullRequestDiff,
)
from mcp_atlassian.utils.http import (
    configure_circuit_breaker,
    configure_concurrency,
    configure_rate_limit,
    configure_retry,
    format_rate_limit_error,
)
from mcp_atlassian.utils.oauth import configure_oauth_session
from mcp_atlassian.utils.proxy import apply_proxy_configuration
from mcp_atlassian.utils.ssl import configure_ssl_verification
from mcp_atlassian.utils.ssrf_adapter import mount_ssrf_pinning
from mcp_atlassian.utils.urls import make_ssrf_redirect_hook
from mcp_atlassian.utils.user_agent import get_default_user_agent

from .config import BitbucketConfig

logger = logging.getLogger("mcp-atlassian.bitbucket")

# Bitbucket Data Center core REST API base path.
API_BASE_PATH = "/rest/api/1.0"

# Max characters of upstream error text taken from a 400/401/403/409
# rejection. The instance's own ``errors[].message`` is actionable, but it must
# be bounded so a pathological upstream body cannot bloat the client-facing
# error message.
_ERROR_MESSAGE_MAX_CHARS = 500
# Cap on the bytes read from a rejection's error body to extract that text. The
# documented error envelope is small; anything larger is not Bitbucket's.
_ERROR_BODY_MAX_BYTES = 64 * 1024

# Default cap on the decoded bytes read from any response body. Every request
# streams its body and stops reading at the cap, so an oversized upstream
# payload is bounded in network transfer and memory before it is parsed.
# Callers pass a lower ``max_response_bytes`` for endpoints whose useful
# payload is smaller.
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# Page size and page cap for the user-slug lookup that resolves the caller's
# X-AUSERNAME to a slug. The ``filter`` query is a substring match, so the exact
# name match can fall on a later page in a large directory; the cap bounds the
# scan so a pathological filter cannot walk the whole user base. The lookup is
# internal to a single tool call and cannot be resumed by the MCP client, so
# it is the client's only multi-request path, and the cap is kept in single
# digits (5 * 100 = 500 filtered users) to bound upstream load.
_USERS_PAGE_SIZE = 100
_MAX_USER_LOOKUP_PAGES = 5


class BitbucketResourceNotFoundError(ValueError):
    """A Bitbucket resource (project/repository/pull request) was not found.

    Raised by :meth:`BitbucketClient._get` on an HTTP 404 so callers can tell a
    missing/no-access resource apart from a generic network or API failure and
    surface an actionable message. Subclasses :class:`ValueError` so existing
    ``except ValueError`` handlers still catch it (degrading to a network-error
    classification) when a caller does not handle it specifically.
    """


class BitbucketResponseTooLargeError(ValueError):
    """A response body exceeded the download cap and was not retrieved.

    Raised by :meth:`BitbucketClient._request` once the streamed body passes
    ``max_response_bytes``. Mixins catch it to append endpoint-specific
    recovery guidance. Subclasses :class:`ValueError` so generic handlers
    still catch it.
    """


# Default number of projects a single list_projects call returns.
DEFAULT_PROJECTS_LIMIT = 25
# Hard ceiling on projects returned in one call. Each call fetches a single
# window whose size is the requested `limit`, so this bounds the per-request
# page size asked of the instance (and the tool's `limit` parameter), with
# truncation surfaced explicitly to the caller.
MAX_PROJECTS_LIMIT = 1000


@dataclass
class BitbucketPage:
    """A bounded, possibly-truncated page of items from a paged DC endpoint.

    The domain-agnostic result of :meth:`BitbucketClient._fetch_page`. See
    :class:`BitbucketProjectsPage` for the full meaning of the ``is_last_page`` /
    ``truncated`` combinations; the semantics are identical, only the field name
    of the collected items differs.

    Attributes:
        values: The collected raw item dicts (at most the requested ``limit``).
        is_last_page: Whether the scan reached the end of the upstream list.
        truncated: Whether the returned ``values`` omits items that exist.
        next_page_start: The ``start`` offset to resume from after this page, or
            ``None`` when there is no forward cursor (the three cases are
            listed under :meth:`BitbucketClient._fetch_page`). Read it with
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
    every visible project iff ``truncated`` is False. The reachable combinations:

    - ``is_last_page=True,  truncated=False``: complete, nothing was omitted.
    - ``is_last_page=False, truncated=True``: more pages exist upstream, so
      resume with ``next_page_start``. With a ``projects_filter`` and an empty
      result this means the window matched nothing, and a later window may
      still match.

    Defensively, ``is_last_page=True, truncated=True`` marks a server that
    returned more items than the requested window (the surplus was trimmed).
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

        # Per-user identity cache for paths that need the caller's slug (the
        # current-user profile, review status). X-AUSERNAME is captured from
        # authenticated responses and the resolved slug is memoised. The client
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

        # Validate redirects for SSRF on every outbound call from this session,
        # and pin DNS resolution against rebinding: resolve+validate once and
        # connect to that address, closing the validate→reconnect TOCTOU.
        self._session.hooks["response"].append(make_ssrf_redirect_hook())
        mount_ssrf_pinning(self._session, self.config.url)

        # Apply opt-in HTTP hardening after SSL setup and after the pinning
        # adapter is mounted: these wrappers patch send() in place on whatever
        # adapters are mounted now, so mounting the pinning adapter later would
        # silently drop them.
        configure_retry(self._session, service="Bitbucket")
        configure_concurrency(self._session, service="Bitbucket")
        configure_rate_limit(self._session, service="Bitbucket")
        configure_circuit_breaker(self._session, service="Bitbucket")

        self._session = apply_proxy_configuration(
            logger=logger,
            service_name="Bitbucket",
            session=self._session,
            config=self.config,
            target_url=self.config.url,
        )

        # Set an explicit User-Agent so requests aren't blocked by WAFs that
        # reject the default ``python-requests/X.Y`` header. User-supplied
        # custom headers below can still override this.
        self._session.headers["User-Agent"] = get_default_user_agent()

        if self.config.custom_headers:
            logger.debug(
                "Applying %d custom headers to Bitbucket session",
                len(self.config.custom_headers),
            )
            for header_name, header_value in self.config.custom_headers.items():
                self._session.headers[header_name] = header_value
                logger.debug("Applied custom header: %s", header_name)

    @property
    def _api_root(self) -> str:
        """Return the Bitbucket DC core REST API root URL.

        Returns:
            The base URL joined with the ``/rest/api/1.0`` path.
        """
        return self._module_root(API_BASE_PATH)

    def _module_root(self, base_path: str) -> str:
        """Return the root URL of one Bitbucket DC REST module.

        Bitbucket DC exposes more than one REST module under ``/rest``: the
        core API at ``/rest/api/1.0`` and, for example, the build-status
        module at ``/rest/build-status/1.0``. Every module shares the host,
        session, and error taxonomy. Only the path prefix differs.

        The prefix is an internal constant, never caller input. Its shape is
        checked as a guard against a mis-typed constant: it must start with a
        single ``/`` and carry no ``?``, ``#``, or ``..``. The check does not
        require a ``/rest/`` prefix, so it relies on callers passing a module
        constant.

        Args:
            base_path: The module's path prefix (e.g. ``API_BASE_PATH``).

        Returns:
            The base URL joined with ``base_path``.

        Raises:
            ValueError: If ``base_path`` is not a plain absolute path prefix.
        """
        if (
            not base_path.startswith("/")
            or base_path.startswith("//")
            or any(token in base_path for token in ("?", "#", ".."))
        ):
            raise ValueError(
                "base_path must be an absolute REST module prefix such as "
                f"{API_BASE_PATH!r}."
            )
        return f"{self.config.url.rstrip('/')}{base_path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        allow_empty: bool = False,
        max_response_bytes: int | None = None,
        base_path: str = API_BASE_PATH,
    ) -> Any:
        """Issue a request against a Bitbucket DC REST module.

        Carries the leak-free error taxonomy shared by every verb:
        :meth:`_get`/:meth:`_post`/:meth:`_put`/:meth:`_delete` are thin
        wrappers. Connection, TLS, timeout, auth (401/403), not-found (404),
        client/conflict (400/409), non-JSON-body, and any other transport
        failure map to a crafted message that omits the raw transport error, so
        internal host/pool details do not reach the client. The sole exception
        is a 400/401/403/409, where the instance's own ``errors[].message``
        text (and nothing else from the body) is appended so a rejected
        request (a write the server refuses, a missing repository permission,
        or a read such as the merge status of a closed pull request) is
        actionable.

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, ``"PUT"``, ``"DELETE"``).
            path: API path relative to ``base_path`` (e.g. ``"/projects"``).
            params: Optional query parameters.
            json_body: Optional JSON request body (writes only). When None no
                body is attached, so a GET issues the exact original call.
            allow_empty: When True, a 2xx with an empty/no-content body returns
                ``None`` instead of raising, for verbs (``DELETE``) whose
                success response is a ``204`` with no JSON. The error taxonomy is
                unchanged; only an empty *success* body is tolerated.
            max_response_bytes: Cap on the decoded body bytes read from the
                response; ``DEFAULT_MAX_RESPONSE_BYTES`` when None. The body is
                streamed and the download aborted once it exceeds the cap, which
                bounds the bytes pulled over the network and buffered in memory.
                The upstream server still generates the full body; only the
                download is bounded.
            base_path: The REST module prefix the path is relative to. The
                core API (``/rest/api/1.0``) unless a caller addresses another
                module (see :meth:`_module_root`).

        Returns:
            The parsed JSON response, or ``None`` for an empty body when
            ``allow_empty`` is set.

        Raises:
            MCPAtlassianAuthenticationError: If the bearer token is rejected
                (HTTP 401/403).
            BitbucketResourceNotFoundError: On HTTP 404 (a ValueError subclass).
            BitbucketResponseTooLargeError: If the body exceeds
                ``max_response_bytes`` (a ValueError subclass).
            ValueError: If the request fails to connect, the TLS handshake
                fails, the request times out, returns another error status, or
                returns a non-JSON body (e.g. an HTML proxy login page on a
                200).
        """
        url = f"{self._module_root(base_path)}{path}"
        # Only a write attaches a body; a GET is issued without ``json=``.
        # Every response is streamed so the byte cap can stop the download.
        request_kwargs: dict[str, Any] = {
            "params": params,
            "timeout": self.config.timeout,
            "stream": True,
        }
        if json_body is not None:
            request_kwargs["json"] = json_body
        cap = (
            DEFAULT_MAX_RESPONSE_BYTES
            if max_response_bytes is None
            else max_response_bytes
        )
        http_method: Callable[..., Any] = getattr(self._session, method.lower())
        response: Any = None
        try:
            response = http_method(url, **request_kwargs)
            self._capture_auth_username(response)
            response.raise_for_status()
            return self._read_capped_json(response, path, cap, allow_empty=allow_empty)
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
                # Bitbucket also answers 401/403 for an authenticated caller
                # who lacks a repository permission (REPO_WRITE to merge). The
                # instance's own errors[].message says which, so it is appended
                # the same bounded way as for a 400/409.
                detail = self._extract_error_messages(e.response, path)
                error_msg = (
                    "Bitbucket authentication failed (HTTP "
                    f"{status}). The forwarded OAuth bearer token was rejected "
                    "or lacks permission for this resource"
                    + (f": {detail}" if detail else ".")
                )
                logger.error(error_msg)
                raise MCPAtlassianAuthenticationError(error_msg) from e
            if status == 404:
                # The path is caller-derived and already percent-encoded, and
                # the upstream body is not echoed.
                error_msg = (
                    f"Bitbucket resource not found (HTTP 404) for {path}. The "
                    "project, repository, or pull request does not exist, or the "
                    "authenticated user lacks permission to view it."
                )
                logger.error(error_msg)
                raise BitbucketResourceNotFoundError(error_msg) from e
            if status in (400, 409):
                # A rejected request, either a write the server refuses (bad
                # anchor, author-self-approve, stale version) or a read it
                # declines (merge status of a closed pull request). Only the
                # instance's own errors[].message is appended so the caller
                # can act on it.
                detail = self._extract_error_messages(e.response, path)
                error_msg = (
                    f"Bitbucket API request to {path} failed with HTTP {status}"
                    + (f": {detail}" if detail else ".")
                )
                logger.error(error_msg)
                raise ValueError(error_msg) from e
            if status == 429:
                # Surface the structured backoff hint (Retry-After when the
                # server set it) so the caller pauses instead of retrying.
                error_msg = format_rate_limit_error(e, service="Bitbucket")
                logger.error(error_msg)
                raise ValueError(error_msg) from e
            error_msg = (
                f"Bitbucket API request to {path} failed with HTTP "
                f"{status if status is not None else 'unknown'}."
            )
            logger.error(error_msg)
            raise ValueError(error_msg) from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # A 2xx with a non-JSON body usually means an intermediary (proxy
            # login page, SSO redirect) answered instead of Bitbucket. Invalid
            # UTF-8 surfaces from json.loads as a codec error, not a JSON one.
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
            # logged server-side only.
            logger.error("Bitbucket request to %s failed: %s", path, e)
            error_msg = f"Bitbucket API request to {path} failed (network error)."
            raise ValueError(error_msg) from e
        finally:
            # A streamed response holds its connection until the body is
            # consumed or the response is closed. An error response is not
            # read in full, so release it here.
            if response is not None:
                response.close()

    @staticmethod
    def _read_capped_json(
        response: Any, path: str, max_bytes: int, *, allow_empty: bool = False
    ) -> Any:
        """Read a streamed response body up to ``max_bytes`` and parse it as JSON.

        Bitbucket DC has no server-side size bound on some single-payload
        responses (the PR diff, a browse window of long lines), so the body is
        streamed and the download aborted once it exceeds the cap; the caller
        closes the response, which releases the connection. The declared
        ``Content-Length``, when present, short-circuits before any body bytes
        are read; it measures the wire size (possibly compressed), so it only
        fires for uncompressed responses or ones whose compressed size already
        exceeds the cap. The chunked read below counts decoded bytes and
        enforces the cap.

        Args:
            response: The streamed (``stream=True``) 2xx response.
            path: The API path, for the error message.
            max_bytes: Maximum number of body bytes to accept.
            allow_empty: When True, an empty or whitespace-only body returns
                ``None`` instead of raising the non-JSON path (a ``204``).

        Returns:
            The parsed JSON body, or ``None`` for an empty body when
            ``allow_empty`` is set.

        Raises:
            BitbucketResponseTooLargeError: If the body exceeds ``max_bytes``.
                The message tells the caller to narrow the request rather than
                retry it.
        """
        too_large_msg = (
            f"Bitbucket response for {path} exceeds the "
            f"{max_bytes / (1024 * 1024):g} MiB download cap and was not "
            "retrieved. Narrow the request (a smaller window or a more "
            "specific path) instead of retrying."
        )
        try:
            declared_length = int(response.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            # Absent or unparseable Content-Length (e.g. chunked transfer
            # encoding): fall through to the capped chunked read below.
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            logger.error(too_large_msg)
            raise BitbucketResponseTooLargeError(too_large_msg)
        chunks: list[bytes] = []
        received = 0
        for chunk in response.iter_content(chunk_size=65536):
            received += len(chunk)
            if received > max_bytes:
                logger.error(too_large_msg)
                raise BitbucketResponseTooLargeError(too_large_msg)
            chunks.append(chunk)
        body = b"".join(chunks)
        # A DELETE succeeds with a 204 and no body; parsing it would raise the
        # non-JSON path and mask the success. Only an empty success body is
        # tolerated, and only when the caller opted in.
        if allow_empty and not body.strip():
            return None
        return json.loads(body)

    @staticmethod
    def _extract_error_messages(response: Any, path: str) -> str | None:
        """Extract the joined ``errors[].message`` text from an error body.

        Bitbucket DC reports request errors with a documented envelope,
        ``{"errors": [{"message": ...}, ...]}``. Only the human-readable
        ``message`` strings are surfaced, joined and length-capped, so a
        400/401/403/409 is actionable. ``exceptionName``, ``context``, the raw body,
        and transport internals are dropped.

        The body is read through the capped streamed reader under
        ``_ERROR_BODY_MAX_BYTES``; a larger or non-JSON body yields None.

        Args:
            response: The error response object.
            path: The API path, for the reader's log message.

        Returns:
            The joined, capped messages, or None if the body is not a JSON
            object of the documented shape, in which case the caller falls back
            to a generic status-only message.
        """
        try:
            body = BitbucketClient._read_capped_json(
                response, path, _ERROR_BODY_MAX_BYTES
            )
        except (ValueError, TypeError, RequestException):
            # The body is optional detail. A stream that breaks while it is
            # read (a RequestException from iter_content) is caught here, so
            # the status-based message is kept and the raw transport error is
            # not exposed.
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
        response and later resolved to a user slug. The header is absent from
        the OpenAPI spec and documented in practice only, so it is read
        defensively and an absent or non-string value leaves the cache
        untouched.
        """
        username = response.headers.get("X-AUSERNAME")
        if isinstance(username, str) and username:
            self._auth_username = username

    @staticmethod
    def _encode_segment(value: str, *, name: str, what: str) -> str:
        """Validate and percent-encode one caller-supplied REST path segment.

        The value is stripped and rejected when blank, ``.``, or ``..``:
        ``quote`` leaves periods unencoded, and the HTTP layer normalises a
        dot segment away, which would silently move the request to a
        different endpoint. The surviving value is percent-encoded with
        ``quote(safe="")`` so it cannot inject an unescaped slash or query
        separator.

        Args:
            value: The caller-supplied segment.
            name: The parameter name used in the error message.
            what: The human-readable kind of value (e.g. ``"project key"``).

        Returns:
            The percent-encoded segment.

        Raises:
            ValueError: If the value is blank, ``.``, or ``..``. Raised before
                any request is issued.
        """
        text = value.strip()
        if not text:
            raise ValueError(f"{name} must be a non-empty Bitbucket {what}.")
        if text in (".", ".."):
            raise ValueError(
                f"{name} must be a Bitbucket {what}, not the path segment {text!r}."
            )
        return quote(text, safe="")

    @staticmethod
    def _repo_base_path(project_key: str, repository_slug: str) -> str:
        """Build and validate the repo-scoped REST path `/projects/{key}/repos/{slug}`.

        Both caller-supplied segments go through :meth:`_encode_segment`, so a
        blank or dot segment is rejected and the survivors are percent-encoded
        (no unescaped slashes) to prevent path traversal.

        Args:
            project_key: The project key (e.g. ``"PROJ"``).
            repository_slug: The repository slug (e.g. ``"my-repo"``).

        Returns:
            ``/projects/{key}/repos/{slug}`` with both caller segments
            percent-encoded.

        Raises:
            ValueError: If either caller segment is blank, ``.``, or ``..``.
        """
        key = BitbucketClient._encode_segment(
            project_key, name="project_key", what="project key"
        )
        slug = BitbucketClient._encode_segment(
            repository_slug, name="repository_slug", what="repository slug"
        )
        return f"/projects/{key}/repos/{slug}"

    @staticmethod
    def _split_repo_ref(value: str, *, name: str) -> tuple[str, str]:
        """Split a ``PROJECT/slug`` repository reference into its two segments.

        Used where a request names another repository of the same hierarchy (a
        fork) as a value rather than a path: the compare ``fromRepo`` query
        parameter and a pull request's source ``repository`` object. Each half
        is validated with the path-segment rules (non-blank, not ``.`` or
        ``..``) so that a traversal-shaped value is rejected before any
        request. The halves are returned stripped and unencoded, since they
        travel as query or JSON values that the HTTP layer encodes.

        Args:
            value: The caller-supplied ``PROJECT/slug`` text.
            name: The parameter name used in the error message.

        Returns:
            The ``(project_key, repository_slug)`` pair.

        Raises:
            ValueError: If the value is not a single ``PROJECT/slug`` pair of
                valid segments.
        """
        parts = value.strip().split("/")
        if len(parts) != 2:
            raise ValueError(
                f"{name} must be a project key and repository slug separated "
                "by one slash (PROJECT/slug)."
            )
        key, slug = parts
        BitbucketClient._encode_segment(key, name=name, what="project key")
        BitbucketClient._encode_segment(slug, name=name, what="repository slug")
        return key.strip(), slug.strip()

    @staticmethod
    def _enum_param(
        value: str | None, *, name: str, allowed: tuple[str, ...]
    ) -> str | None:
        """Normalize an enum-valued query parameter.

        A blank value is dropped so the server default applies; any other
        value is stripped, matched case-insensitively against ``allowed``, and
        returned in the case the endpoint expects. An unrecognised value
        raises before any request is issued, since the instance would reject
        it with an opaque 400.

        Args:
            value: The caller-supplied value, or None.
            name: The parameter name used in the error message.
            allowed: The accepted values, in the endpoint's case.

        Returns:
            The normalized value, or None for a None or blank input.

        Raises:
            ValueError: If the value is not one of ``allowed``.
        """
        if value is None or not value.strip():
            return None
        text = value.strip()
        for candidate in allowed:
            if text.casefold() == candidate.casefold():
                return candidate
        raise ValueError(f"{name} must be one of {', '.join(allowed)}.")

    @staticmethod
    def _encode_repo_path(path: str | None, *, what: str = "path") -> str:
        """Percent-encode a caller-supplied repository file path.

        The path is split on ``/``; an empty, ``.``, or ``..`` component is
        rejected so the value cannot traverse outside the intended resource or
        double up separators. Each surviving component is percent-encoded with
        ``quote(safe="")`` so it cannot inject an unescaped slash or query
        separator, and the components are rejoined with ``/``.

        Args:
            path: The caller-supplied path. An empty or blank value yields an
                empty string.
            what: The parameter name used in the error message.

        Returns:
            The encoded path with slashes preserved between components, or an
            empty string for a blank input.

        Raises:
            ValueError: If any component is empty, ``.``, or ``..``. Raised
                before any request is issued.
        """
        if path is None or not path.strip():
            return ""
        encoded: list[str] = []
        for component in path.split("/"):
            if component in ("", ".", ".."):
                raise ValueError(
                    f"Invalid component in {what}: empty, '.', and '..' "
                    "segments are not allowed (path traversal rejected)."
                )
            encoded.append(quote(component, safe=""))
        return "/".join(encoded)

    @staticmethod
    def _coerce_pr_id(pull_request_id: int | str) -> int:
        """Coerce and validate a pull-request id to a positive integer.

        Shared by the pull-request and commit mixins (both build
        ``.../pull-requests/{id}/...`` paths).

        Args:
            pull_request_id: The caller-supplied pull-request id.

        Returns:
            The id as a positive ``int``, safe to interpolate into the path
            since an integer cannot carry traversal or injection.

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

    @staticmethod
    def _build_diff(
        data: Any,
        url: str,
        *,
        single_file: bool,
        max_lines_per_file: int,
        max_files: int,
    ) -> BitbucketPullRequestDiff:
        """Build a bounded diff model from a ``diff`` endpoint body.

        Shared by the pull-request and compare diff endpoints. Two body
        shapes are accepted for either form: the ``RestDiffResponse``
        envelope (one ``RestDiff`` per file under ``diffs``), which the
        pull-request endpoint returns at runtime, and a bare ``RestDiff``,
        which the Bitbucket Data Center 9.4 REST specification declares for
        the single-file form and for the whole comparison. A bare diff is
        wrapped as a one-file envelope. A non-empty body of any other shape
        raises, so an unrecognised body is not reported as an empty diff.

        Args:
            data: The decoded JSON body.
            url: The request path, for error messages.
            single_file: Whether the single-file form was called (a ``path``
                was appended to the endpoint).
            max_lines_per_file: Per-file diff-line cap, already clamped.
            max_files: File cap for the whole-diff form, already clamped.
                Ignored when ``single_file`` is set.

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketPullRequestDiff`.

        Raises:
            ValueError: If the body is not a JSON object, or a non-empty body
                is neither a diff object nor a ``diffs`` envelope.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{url} (expected a diff object)."
            )
        if single_file:
            max_files = 1
        if isinstance(data.get("diffs"), list):
            return BitbucketPullRequestDiff.from_api_response(
                data, max_lines_per_file=max_lines_per_file, max_files=max_files
            )
        if not single_file and not data:
            return BitbucketPullRequestDiff.from_api_response(
                data, max_lines_per_file=max_lines_per_file, max_files=max_files
            )
        if not any(key in data for key in ("hunks", "source", "destination")):
            raise ValueError(
                "Bitbucket returned an unexpected diff response shape for "
                f"{url} (neither a diff object nor a 'diffs' list), so it is "
                "not reported as an empty diff."
            )
        file_diff = BitbucketFileDiff.from_api_response(
            data, max_lines_per_file=max_lines_per_file
        )
        return BitbucketPullRequestDiff(
            files=[file_diff],
            total_files=1,
            truncated=file_diff.line_truncated or file_diff.server_truncated,
        )

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_response_bytes: int | None = None,
        base_path: str = API_BASE_PATH,
    ) -> Any:
        """Issue a GET against a Bitbucket DC REST module.

        Thin wrapper over :meth:`_request`; see it for the shared error
        taxonomy.

        Args:
            path: API path relative to ``base_path`` (e.g. ``"/projects"``).
            params: Optional query parameters.
            max_response_bytes: Optional cap on the downloaded body size (see
                :meth:`_request`).
            base_path: The REST module prefix, the core API unless given (see
                :meth:`_request`).

        Returns:
            The parsed JSON response.
        """
        return self._request(
            "GET",
            path,
            params=params,
            max_response_bytes=max_response_bytes,
            base_path=base_path,
        )

    def _post(
        self, path: str, *, json_body: Any, params: dict[str, Any] | None = None
    ) -> Any:
        """Issue a POST against the Bitbucket DC core REST API.

        Thin wrapper over :meth:`_request`; see it for the shared error
        taxonomy. On a 400/409 the raised ValueError carries the instance's own
        ``errors[].message`` text, so a write rejection is actionable.

        Args:
            path: API path relative to ``/rest/api/1.0``.
            json_body: The JSON request body.
            params: Optional query parameters, for endpoints that take the
                optimistic-lock ``version`` in the query string as well as the
                body (pull-request merge, decline, and reopen).

        Returns:
            The parsed JSON response.
        """
        return self._request("POST", path, json_body=json_body, params=params)

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

    def _delete(self, path: str, *, params: dict[str, Any] | None = None) -> None:
        """Issue a DELETE against the Bitbucket DC core REST API.

        Thin wrapper over :meth:`_request`; see it for the shared error taxonomy.
        A successful DELETE returns ``204`` with no body, so the empty response
        is tolerated (``allow_empty``) and ``None`` is returned rather than
        raising the non-JSON-body path. A 2xx that carries a JSON body other
        than an empty object or list matches no documented success shape and
        is reported as an unconfirmed delete. On a 400/409 the raised ValueError carries the
        instance's own ``errors[].message`` text, so a rejection (e.g. a stale
        ``version`` or a comment with replies) is actionable.

        Args:
            path: API path relative to ``/rest/api/1.0``.
            params: Optional query parameters (e.g. the optimistic-lock
                ``version``).

        Returns:
            None, since a successful delete has no body to return.

        Raises:
            ValueError: If the 2xx response carries a JSON body other than an
                empty object or list (the delete may have been applied but was
                not confirmed), or the
                request fails (see :meth:`_request`).
        """
        body = self._request("DELETE", path, params=params, allow_empty=True)
        if body:
            raise ValueError(
                f"Bitbucket returned a body for DELETE {path}; expected an "
                "empty 204 response. The delete may have been applied but was "
                "not confirmed."
            )

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
            # Cap the echoed body so an intermediary cannot inflate the error.
            raise MCPAtlassianAuthenticationError(
                "Unexpected Bitbucket validation response: "
                + repr(result)[:_ERROR_MESSAGE_MAX_CHARS]
            )
        return result

    def _resolve_current_user_slug(self) -> str:
        """Resolve the authenticated caller's user slug, memoised per client.

        Bitbucket DC's REST surface has no self/whoami endpoint, so the caller's
        identity is learned in two steps: the username comes from the
        ``X-AUSERNAME`` header (primed here with a cheap authenticated call if no
        prior response has set it), then the *slug* that user-scoped paths need is
        resolved from ``GET /users?filter=<username>`` by exact-matching the
        ``name`` field. The slug is cached for the life of this (per-user) client.

        Returns:
            The authenticated user's slug.

        Raises:
            ValueError: If the instance does not expose ``X-AUSERNAME`` (the
                caller identity is unknown) or no user exactly matches it.
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
                "instance did not return an X-AUSERNAME header."
            )

        # The filter is a substring match, so several users can come back and the
        # exact match may fall on a later page; walk pages until it is found or
        # the list is exhausted, taking only the exact name match. The page cap
        # bounds a pathological filter.
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
            is_last_page, next_start = self._page_cursor(
                result, start=start, path="/users"
            )
            if is_last_page or next_start is None:
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
                list of objects. A missing or misshaped body is reported as an
                error rather than as an empty result, identically whether or
                not a per-page transform is applied (a non-dict entry would
                otherwise crash the transform).
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

    @staticmethod
    def _page_cursor(
        page: dict[str, Any], *, start: int, path: str
    ) -> tuple[bool, int | None]:
        """Validate a paged envelope's completion flag and resume cursor.

        Shared by every single-window read (the ``values`` envelope in
        :meth:`_fetch_page` and the ``browse`` envelopes) so a cursor is
        handed back under one rule: only when the page is not the last one and
        ``nextPageStart`` is an integer beyond ``start``.

        Args:
            page: The parsed envelope carrying ``isLastPage`` and, on a
                non-last page, ``nextPageStart``.
            start: The offset this window was requested at.
            path: The API path or browse path, for the error message.

        Returns:
            ``(is_last_page, next_page_start)``. ``next_page_start`` is ``None``
            on the last page and on a non-last page whose cursor is missing,
            not an integer, or does not advance past ``start`` (resuming from
            such a cursor would refetch the same window, so the page is
            reported incomplete and not resumable).

        Raises:
            ValueError: If ``isLastPage`` is missing or not a boolean. A
                missing flag would otherwise read as a complete list, and a
                string such as ``"false"`` would otherwise read as true.
        """
        is_last_page = page.get("isLastPage")
        if not isinstance(is_last_page, bool):
            raise ValueError(
                f"Bitbucket returned an unexpected response shape for {path}; "
                "expected a boolean 'isLastPage'."
            )
        if is_last_page:
            return True, None
        raw_cursor = page.get("nextPageStart")
        if (
            isinstance(raw_cursor, int)
            and not isinstance(raw_cursor, bool)
            and raw_cursor > start
        ):
            return False, raw_cursor
        return False, None

    def _fetch_page(
        self,
        path: str,
        *,
        limit: int,
        start: int = 0,
        params: dict[str, Any] | None = None,
        transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        base_path: str = API_BASE_PATH,
    ) -> BitbucketPage:
        """Fetch one window of a Bitbucket DC paged endpoint.

        Bitbucket DC paginates with ``start``/``limit`` query params and answers
        with a ``{values, isLastPage, nextPageStart, ...}`` envelope. This issues
        one upstream request per call and surfaces the envelope's own cursor,
        so the caller (ultimately the MCP client) resumes with
        ``start=next_page_start``. The server does not walk pages on the
        caller's behalf. ``transform`` is applied to the window's
        ``values`` before they are returned (e.g. a client-side allowlist
        filter); because it only narrows the single window, a filtered window can
        legitimately be empty while ``next_page_start`` still points at the next
        window.

        Args:
            path: API path relative to ``base_path`` (e.g. ``"/projects"``).
            limit: The window size, sent as the ``limit`` query param; the result
                is sliced to it. Callers clamp this to their own domain ceiling
                first.
            start: The offset of the window to fetch (a resume cursor). 0 starts
                from the beginning.
            params: Extra query params merged into the request.
            transform: Optional narrowing of the window's ``values`` list.
            base_path: The REST module prefix, the core API unless given (see
                :meth:`_request`).

        Returns:
            A :class:`BitbucketPage` with the window's items (at most ``limit``),
            whether the upstream list was fully consumed (``is_last_page``),
            whether items were omitted because a bound was hit (``truncated``),
            and ``next_page_start``, the upstream cursor to resume from. It is
            ``None`` in three cases: the upstream list ended (``is_last_page``
            True), the server returned more items than requested and the
            surplus was trimmed (``truncated`` True, with the trimmed tail
            inside this window and unreachable by a forward cursor), or a
            non-last page advertised no usable cursor (``truncated`` True,
            incomplete and unresumable). A non-null ``next_page_start`` is the
            exact upstream offset to resume from.

        Raises:
            ValueError: If the page is not a paged object with a ``values`` list,
                or if the request fails (see :meth:`_get`).
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        page_params = {**(params or {}), "start": start, "limit": limit}
        page = self._get(path, params=page_params, base_path=base_path)
        values = self._page_values(page, path)
        if transform is not None:
            values = transform(values)

        is_last_page, cursor = self._page_cursor(page, start=start, path=path)

        # Defensive: a server answering with more items than the requested
        # ``limit``. The trimmed tail lives inside this window, so a forward
        # cursor cannot recover it without re-yielding the window; advertise no
        # cursor and mark the result truncated.
        overshot = len(values) > limit
        truncated = overshot or not is_last_page
        next_page_start = None if overshot else cursor
        return BitbucketPage(
            values=values[:limit],
            is_last_page=is_last_page,
            truncated=truncated,
            next_page_start=next_page_start,
        )
