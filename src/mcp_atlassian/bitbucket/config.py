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
from ..utils.urls import is_atlassian_cloud_url


@dataclass
class BitbucketConfig:
    """Bitbucket Data Center API configuration.

    This client targets Bitbucket Data Center with OAuth 2.0. Bitbucket DC also
    supports PAT and basic auth; this implementation does not wire those up yet.
    The server holds a bring-your-own-token OAuth config at startup and forwards
    each request's user OAuth bearer token into a per-user config (see
    ``mcp_atlassian.servers.dependencies``).
    """

    url: str  # Base URL for Bitbucket Data Center
    auth_type: Literal["oauth"]  # OAuth only for now; DC also supports PAT/basic
    oauth_config: OAuthConfig | BYOAccessTokenOAuthConfig | None = None
    ssl_verify: bool = True  # Whether to verify SSL certificates
    projects_filter: str | None = None  # Comma-separated project keys to filter
    http_proxy: str | None = None  # HTTP proxy URL
    https_proxy: str | None = None  # HTTPS proxy URL
    no_proxy: str | None = None  # Comma-separated list of hosts to bypass proxy
    socks_proxy: str | None = None  # SOCKS proxy URL (optional)
    custom_headers: dict[str, str] | None = None  # Custom HTTP headers
    timeout: int = 75  # Connection timeout in seconds

    @property
    def is_cloud(self) -> bool:
        """Check if this is a cloud instance.

        Returns:
            Always False — this client targets Bitbucket Data Center only.
        """
        return False

    @property
    def is_data_center(self) -> bool:
        """Check if this is a Data Center instance.

        Returns:
            Always True — this client targets Bitbucket Data Center only.
        """
        return True

    @property
    def verify_ssl(self) -> bool:
        """Compatibility property mirroring the Jira/Confluence configs.

        Returns:
            The ssl_verify value.
        """
        return self.ssl_verify

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

        # Data Center only. Reject a Cloud URL rather than silently treating it
        # as DC (is_data_center is hardcoded True on this config).
        if is_atlassian_cloud_url(url):
            error_msg = (
                f"BITBUCKET_URL '{url}' looks like a Bitbucket Cloud URL. "
                "This client supports Bitbucket Data Center (self-hosted) only; "
                "set BITBUCKET_URL to your Data Center base URL."
            )
            raise ValueError(error_msg)

        # Data Center OAuth only. A BYO access token takes precedence over the
        # full OAuth config (matches get_oauth_config_from_env order).
        oauth_config = get_oauth_config_from_env(
            service_url=url, service_type="bitbucket"
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

        timeout = 75
        timeout_env = os.getenv("BITBUCKET_TIMEOUT")
        if timeout_env:
            try:
                timeout = int(timeout_env)
            except ValueError:
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
            # Bring-your-own-token OAuth (user-provided tokens via headers):
            # empty client credentials are valid — the real token arrives per
            # request.
            if isinstance(self.oauth_config, OAuthConfig):
                if (
                    not self.oauth_config.client_id
                    and not self.oauth_config.client_secret
                ):
                    return True
                # DC OAuth needs client_id + client_secret (no cloud_id).
                return bool(
                    self.oauth_config.client_id and self.oauth_config.client_secret
                )
            # BYO access token: the token itself is enough for DC.
            if isinstance(self.oauth_config, BYOAccessTokenOAuthConfig):
                return bool(self.oauth_config.access_token)
        logger.warning("Incomplete Bitbucket OAuth configuration detected")
        return False
