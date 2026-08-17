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
from ..utils.proxy import get_proxy_settings_from_env
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
    # Comma-separated project keys limiting project discovery/listing only
    # (see ProjectsMixin._projects_filter_keys for the scope of the filter)
    projects_filter: str | None = None
    http_proxy: str | None = None  # HTTP proxy URL
    https_proxy: str | None = None  # HTTPS proxy URL
    no_proxy: str | None = None  # Comma-separated list of hosts to bypass proxy
    socks_proxy: str | None = None  # SOCKS proxy URL (optional)
    proxy_wpad_enable: bool = False  # Whether to load PAC/WPAD configuration
    proxy_wpad_url: str | None = None  # PAC URL used when WPAD is enabled
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

        # This config builds only Data Center endpoints, so a Cloud URL is
        # rejected rather than used to build them against a Cloud host.
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
        # shared ATLASSIAN_OAUTH_* credentials must not satisfy it, which keeps
        # the loader aligned with the availability gate in utils.environment.
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

        proxy_settings = get_proxy_settings_from_env("BITBUCKET")

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
            http_proxy=proxy_settings["http_proxy"],
            https_proxy=proxy_settings["https_proxy"],
            no_proxy=proxy_settings["no_proxy"],
            socks_proxy=proxy_settings["socks_proxy"],
            proxy_wpad_enable=bool(proxy_settings["proxy_wpad_enable"]),
            proxy_wpad_url=proxy_settings["proxy_wpad_url"],
            custom_headers=custom_headers,
            timeout=timeout,
        )

    def is_auth_configured(self) -> bool:
        """Check whether the OAuth configuration is complete enough to use.

        Client id + secret alone count as configured even though they cannot
        mint an access token by themselves; the failure is deferred to the
        first client construction, which raises when no token source (OAuth
        proxy, per-request bearer, or a pre-seeded token cache) supplies one.
        This mirrors the Jira/Confluence DC OAuth contract.

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
