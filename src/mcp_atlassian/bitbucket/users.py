"""Bitbucket Data Center user operations."""

import logging
from urllib.parse import quote

from ..models.bitbucket import BitbucketUserProfile
from .client import BitbucketClient, BitbucketResourceNotFoundError

logger = logging.getLogger("mcp-atlassian.bitbucket")


class UsersMixin(BitbucketClient):
    """Mixin for Bitbucket Data Center user operations."""

    def get_current_user_profile(
        self, *, refresh: bool = False
    ) -> BitbucketUserProfile:
        """Get the authenticated caller's user profile.

        Bitbucket DC has no self/whoami endpoint, so the caller's slug is
        resolved first (see
        :meth:`~mcp_atlassian.bitbucket.client.BitbucketClient._resolve_current_user_slug`)
        and then ``GET /users/{userSlug}`` is read. The profile read happens on
        every call so the result is live. The slug resolution is memoised on
        this client, so its cost (a priming request plus a bounded
        user-directory lookup) is paid once per client instance: once per
        process on a long-lived stdio client, and once per call on a
        stateless HTTP transport that builds a client per request.

        Args:
            refresh: Discard the memoised username and slug and resolve them
                again before the profile read. Use it on a long-lived client
                after the account has been renamed.

        Returns:
            A :class:`~mcp_atlassian.models.bitbucket.BitbucketUserProfile`.

        Raises:
            ValueError: If the caller's identity cannot be resolved, the
                response is not a non-empty user object, or the request fails.
            BitbucketResourceNotFoundError: If the resolved slug no longer
                names a visible user.
            MCPAtlassianAuthenticationError: If the bearer token is rejected.
        """
        if refresh:
            self._auth_username = None
            self._current_user_slug = None
        user_slug = self._resolve_current_user_slug()
        # The slug is server-supplied, so an unusable value is reported as a
        # response-shape problem. Dot segments would be normalised
        # away by the HTTP layer and move the request to another endpoint.
        slug = user_slug.strip()
        if not slug or slug in (".", ".."):
            raise ValueError(
                "Bitbucket returned an unexpected response shape: the resolved "
                f"user slug {user_slug!r} is not usable as a path segment."
            )
        path = f"/users/{quote(slug, safe='')}"
        try:
            data = self._get(path)
        except BitbucketResourceNotFoundError as e:
            raise BitbucketResourceNotFoundError(
                f"Bitbucket resource not found (HTTP 404) for {path}: the "
                "resolved user slug no longer names a visible account. It may "
                "have been renamed or removed. Call again with refresh=true to "
                "resolve it again."
            ) from e
        if not isinstance(data, dict) or not data:
            raise ValueError(
                "Bitbucket returned an unexpected response shape for "
                f"{path}; expected a non-empty user object."
            )
        return BitbucketUserProfile.from_api_response(data)
