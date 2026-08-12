"""Configuration module for Bitbucket Data Center API interactions."""

import logging
import os
from dataclasses import dataclass
from typing import Literal

from ..utils.env import get_custom_headers, is_env_ssl_verify
from ..utils.oauth import (
    BYOAccessTokenOAuthConfig,
    OAuthConfig,
    get_oauth_config_from_env,
)
from ..utils.urls import is_atlassian_cloud_url, is_bitbucket_cloud_url

# Connection timeout in seconds when BITBUCKET_TIMEOUT is unset or invalid.
DEFAULT_TIMEOUT_SECONDS = 75


@dataclass
class BitbucketConfig:
    """Bitbucket Data Center API configuration.

    This client targets Bitbucket Data Center with OAuth 2.0. Bitbucket DC also
    supports PAT and basic auth; this implementation does not wire those up.
    The server holds a bring-your-own-token OAuth config at startup and forwards
    each request's user OAuth bearer token into a per-user config (see
    ``mcp_atlassian.servers.dependencies``).
    """

    url: str  # Base URL for Bitbucket Data Center
    auth_type: Literal["oauth"]  # Only OAuth is wired up; DC also supports PAT/basic
    oauth_config: OAuthConfig | BYOAccessTokenOAuthConfig | None = None
    ssl_verify: bool = True  # Whether to verify SSL certificates
    projects_filter: str | None = None  # Comma-separated project keys to filter
    http_proxy: str | None = None  # HTTP proxy URL
    https_proxy: str | None = None  # HTTPS proxy URL
    no_proxy: str | None = None  # Comma-separated list of hosts to bypass proxy
    socks_proxy: str | None = None  # SOCKS proxy URL (optional)
    custom_headers: dict[str, str] | None = None  # Custom HTTP headers
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "BitbucketConfig":
        """Create configuration from environment variables.

        Returns:
            BitbucketConfig with values from environment variables.

        Raises:
            ValueError: If required environment variables are missing or OAuth
                is not configured.
        """
        url = os.getenv("BITBUCKET_URL")
        if not url:
            error_msg = (
                "Missing required BITBUCKET_URL environment variable. "
                "Set BITBUCKET_URL to your Bitbucket Data Center base URL, "
                "for example https://bitbucket.your-company.com"
            )
            raise ValueError(error_msg)

        # Cloud is not handled, and this config only builds DC-shaped
        # endpoints. Reject a Cloud URL rather than silently building DC
        # endpoints against a Cloud host.
        # is_bitbucket_cloud_url covers bitbucket.org (the host a Bitbucket user
        # would actually type); is_atlassian_cloud_url covers Jira/Confluence
        # Cloud hosts, which the Bitbucket check does not match.
        if is_atlassian_cloud_url(url) or is_bitbucket_cloud_url(url):
            error_msg = (
                f"BITBUCKET_URL '{url}' looks like a Cloud URL. "
                "This client builds Bitbucket Data Center endpoints and does "
                "not handle Cloud; point BITBUCKET_URL at a Data Center "
                "base URL."
            )
            raise ValueError(error_msg)

        # Data Center OAuth only. A BYO access token takes precedence over the
        # full OAuth config (matches get_oauth_config_from_env order).
        # Bitbucket is a separate OAuth provider on a separate host, so the
        # shared ATLASSIAN_OAUTH_* credentials must not satisfy it — keep the
        # loader aligned with the availability gate in utils.environment.
        oauth_config = get_oauth_config_from_env(
            service_url=url,
            service_type="bitbucket",
            disallow_shared_fallback=True,
        )
        if not oauth_config:
            error_msg = (
                "Bitbucket authentication is not configured. This "
                "implementation authenticates via OAuth 2.0. "
                "Set BITBUCKET_OAUTH_CLIENT_ID and BITBUCKET_OAUTH_CLIENT_SECRET "
                "(or BITBUCKET_OAUTH_ACCESS_TOKEN), or enable user-provided "
                "tokens with ATLASSIAN_OAUTH_ENABLE=true."
            )
            raise ValueError(error_msg)

        ssl_verify = is_env_ssl_verify("BITBUCKET_SSL_VERIFY")

        projects_filter = os.getenv("BITBUCKET_PROJECTS_FILTER")

        http_proxy = os.getenv("BITBUCKET_HTTP_PROXY", os.getenv("HTTP_PROXY"))
        https_proxy = os.getenv("BITBUCKET_HTTPS_PROXY", os.getenv("HTTPS_PROXY"))
        no_proxy = os.getenv("BITBUCKET_NO_PROXY", os.getenv("NO_PROXY"))
        socks_proxy = os.getenv("BITBUCKET_SOCKS_PROXY", os.getenv("SOCKS_PROXY"))

        custom_headers = get_custom_headers("BITBUCKET_CUSTOM_HEADERS")

        timeout = DEFAULT_TIMEOUT_SECONDS
        timeout_env = os.getenv("BITBUCKET_TIMEOUT")
        if timeout_env:
            try:
                parsed_timeout = int(timeout_env)
            except ValueError:
                parsed_timeout = 0
            if parsed_timeout > 0:
                timeout = parsed_timeout
            else:
                logger = logging.getLogger("mcp-atlassian.bitbucket.config")
                logger.warning(
                    "Invalid BITBUCKET_TIMEOUT value %r; falling back to %d seconds.",
                    timeout_env,
                    timeout,
                )

        return cls(
            url=url,
            auth_type="oauth",
            oauth_config=oauth_config,
            ssl_verify=ssl_verify,
            projects_filter=projects_filter,
            http_proxy=http_proxy,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
            socks_proxy=socks_proxy,
            custom_headers=custom_headers,
            timeout=timeout,
        )

    def is_auth_configured(self) -> bool:
        """Check whether the OAuth configuration is complete enough to use.

        Returns:
            True if authentication is configured, False otherwise.
        """
        logger = logging.getLogger("mcp-atlassian.bitbucket.config")
        if self.auth_type == "oauth" and self.oauth_config:
            if isinstance(self.oauth_config, OAuthConfig):
                client_id = self.oauth_config.client_id
                client_secret = self.oauth_config.client_secret
                # Bring-your-own-token OAuth (user-provided tokens via
                # headers): empty client credentials are valid because the
                # real token arrives per request.
                if not client_id and not client_secret:
                    return True
                # DC OAuth needs client_id + client_secret (no cloud_id). One
                # without the other falls through to the warning.
                if client_id and client_secret:
                    return True
            # BYO access token: the token itself is enough for DC.
            elif self.oauth_config.access_token:
                return True
        logger.warning("Incomplete Bitbucket OAuth configuration detected")
        return False
