"""Resolve the single upstream OAuth provider that the proxy fronts.

The OAuth proxy is provider-agnostic at the protocol level: it fronts exactly
one upstream authorization server and brokers Dynamic Client Registration,
discovery, and the authorization-code exchange for it. Which provider that is
depends only on which environment variables are set.

This module centralises that decision so both the proxy builder
(``servers.main``) and the per-request fetcher layer (``servers.dependencies``)
agree on a single answer to "which provider does the proxy front?". Keeping it
in a leaf module (it imports only ``utils``) avoids an import cycle between
those two modules.

A proxy instance fronts one provider. If the environment configures OAuth
client credentials for more than one provider family, ``proxy_upstream_provider``
raises so the server refuses to start rather than guessing — run a separate
instance per product instead.
"""

import logging
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from mcp_atlassian.utils.env import is_env_truthy
from mcp_atlassian.utils.oauth import (
    CLOUD_AUTHORIZE_URL,
    CLOUD_TOKEN_URL,
    DC_AUTHORIZE_PATH,
    DC_TOKEN_PATH,
)
from mcp_atlassian.utils.urls import (
    is_atlassian_cloud_url,
    is_bitbucket_cloud_url,
)

logger = logging.getLogger("mcp-atlassian.server.oauth-upstream")

# Enables the OAuth proxy / DCR routes. Without it the server exposes no
# authorization-server surface and runs in plain bearer-forwarding mode.
OAUTH_PROXY_ENABLE_ENV = "ATLASSIAN_OAUTH_PROXY_ENABLE"

# Provider families the proxy can front. Jira and Confluence share one Atlassian
# identity provider, so they count as a single "atlassian" upstream; Bitbucket
# Data Center is a distinct provider on its own host.
ProxyProvider = Literal["atlassian", "bitbucket"]


@dataclass(frozen=True)
class ProxyUpstream:
    """The resolved upstream OAuth provider for the proxy.

    Carries only what the proxy builder needs that is provider-specific. The
    deployment-level configuration (public base URL, redirect path, allowed
    client redirect URIs, consent) is resolved separately and is the same
    regardless of provider.
    """

    provider: ProxyProvider
    instance_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str]
    is_cloud: bool
    authorization_endpoint: str
    token_endpoint: str
    extra_authorize_params: dict[str, str] | None


def _parse_scopes(scope_env: str) -> list[str]:
    """Split a space- or comma-separated scope string into a clean list."""
    return [s for part in scope_env.replace(",", " ").split() if (s := part)]


def _resolve_dc_endpoints(instance_url: str) -> tuple[str, str]:
    """Build Data Center OAuth authorize/token endpoints for an instance URL."""
    base_url = instance_url.rstrip("/")
    return f"{base_url}{DC_AUTHORIZE_PATH}", f"{base_url}{DC_TOKEN_PATH}"


def _is_cloud_instance(instance_url: str) -> bool:
    """Return True when the instance URL is an Atlassian Cloud host."""
    parsed_host = (urlparse(instance_url).hostname or "").lower()
    return is_atlassian_cloud_url(instance_url) or parsed_host == "auth.atlassian.com"


def _resolve_upstream_oauth_endpoints(instance_url: str) -> tuple[str, str]:
    """Resolve authorize/token endpoints, branching on Cloud vs Data Center."""
    if _is_cloud_instance(instance_url):
        return CLOUD_AUTHORIZE_URL, CLOUD_TOKEN_URL
    return _resolve_dc_endpoints(instance_url)


def _atlassian_client_id() -> str | None:
    """The configured Atlassian (Jira/Confluence) OAuth client id, if any."""
    return (
        os.getenv("ATLASSIAN_OAUTH_CLIENT_ID")
        or os.getenv("JIRA_OAUTH_CLIENT_ID")
        or os.getenv("CONFLUENCE_OAUTH_CLIENT_ID")
    )


def _bitbucket_client_id() -> str | None:
    """The configured Bitbucket OAuth client id, if any."""
    return os.getenv("BITBUCKET_OAUTH_CLIENT_ID")


def proxy_upstream_provider() -> ProxyProvider | None:
    """Identify the single provider the OAuth proxy fronts.

    The presence of OAuth *client credentials* for a provider family is the
    signal that the proxy is meant to front it. Exactly one family may be
    configured at a time.

    Returns:
        ``"atlassian"`` or ``"bitbucket"`` when that one provider is configured,
        or ``None`` when the proxy is disabled or no client credentials are set.

    Raises:
        RuntimeError: If client credentials for both provider families are set.
            One proxy can front only one provider; run a separate server
            instance per product.
    """
    if not is_env_truthy(OAUTH_PROXY_ENABLE_ENV, "false"):
        return None

    has_atlassian = bool(_atlassian_client_id())
    has_bitbucket = bool(_bitbucket_client_id())

    if has_atlassian and has_bitbucket:
        raise RuntimeError(
            "OAuth proxy is configured for more than one upstream provider: "
            "both Atlassian (Jira/Confluence) and Bitbucket OAuth client "
            "credentials are set. A proxy instance can front exactly one "
            "provider. Run a separate server instance per product, each with "
            "only that product's OAuth client credentials."
        )

    if has_bitbucket:
        return "bitbucket"
    if has_atlassian:
        return "atlassian"
    return None


def _resolve_atlassian_upstream() -> ProxyUpstream | None:
    """Resolve the Atlassian (Jira/Confluence) upstream from the environment."""
    instance_url = (
        os.getenv("ATLASSIAN_OAUTH_INSTANCE_URL")
        or os.getenv("JIRA_URL")
        or os.getenv("CONFLUENCE_URL")
    )
    client_id = _atlassian_client_id()
    redirect_uri = os.getenv("ATLASSIAN_OAUTH_REDIRECT_URI")
    scope_env = os.getenv("ATLASSIAN_OAUTH_SCOPE", "")

    if not all([instance_url, client_id, redirect_uri]):
        logger.warning(
            "OAuth proxy requested but non-secret configuration is incomplete."
        )
        return None

    client_secret = (
        os.getenv("ATLASSIAN_OAUTH_CLIENT_SECRET")
        or os.getenv("JIRA_OAUTH_CLIENT_SECRET")
        or os.getenv("CONFLUENCE_OAUTH_CLIENT_SECRET")
    )
    if not client_secret:
        logger.warning("OAuth proxy requested but client secret is not configured.")
        return None

    is_cloud = _is_cloud_instance(instance_url)
    authorize, token = _resolve_upstream_oauth_endpoints(instance_url)
    return ProxyUpstream(
        provider="atlassian",
        instance_url=instance_url,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=_parse_scopes(scope_env),
        is_cloud=is_cloud,
        authorization_endpoint=authorize,
        token_endpoint=token,
        extra_authorize_params=(
            {"audience": "api.atlassian.com", "prompt": "consent"} if is_cloud else None
        ),
    )


def _resolve_bitbucket_upstream() -> ProxyUpstream | None:
    """Resolve the Bitbucket Data Center upstream from the environment.

    Bitbucket Data Center is self-hosted, so its OAuth provider always uses the
    Data Center endpoint shape. The redirect URI falls back to the shared
    ``ATLASSIAN_OAUTH_REDIRECT_URI`` (which addresses this server, not the
    upstream). The scope env is Bitbucket-specific with no shared fallback, so a
    scope string meant for a different provider cannot leak in.
    """
    instance_url = os.getenv("BITBUCKET_URL")
    client_id = _bitbucket_client_id()
    redirect_uri = os.getenv("BITBUCKET_OAUTH_REDIRECT_URI") or os.getenv(
        "ATLASSIAN_OAUTH_REDIRECT_URI"
    )
    scope_env = os.getenv("BITBUCKET_OAUTH_SCOPE", "")

    if not all([instance_url, client_id, redirect_uri]):
        logger.warning(
            "OAuth proxy requested for Bitbucket but non-secret configuration "
            "is incomplete."
        )
        return None

    # Cloud is not handled yet. A Cloud-looking BITBUCKET_URL would resolve to
    # Data Center-shaped endpoints on a Cloud host, so reject it rather than
    # build a broken upstream. is_atlassian_cloud_url covers Jira/Confluence
    # Cloud hosts; is_bitbucket_cloud_url covers bitbucket.org, the host a
    # Bitbucket user would actually type (which the Atlassian check misses).
    if is_atlassian_cloud_url(instance_url) or is_bitbucket_cloud_url(instance_url):
        logger.warning(
            "BITBUCKET_URL %s looks like a Cloud URL; this server builds "
            "Bitbucket Data Center OAuth endpoints and does not front Bitbucket "
            "Cloud yet, so refusing rather than building DC endpoints against a "
            "Cloud host.",
            instance_url,
        )
        return None

    client_secret = os.getenv("BITBUCKET_OAUTH_CLIENT_SECRET")
    if not client_secret:
        logger.warning(
            "OAuth proxy requested for Bitbucket but client secret is not configured."
        )
        return None

    authorize, token = _resolve_dc_endpoints(instance_url)
    return ProxyUpstream(
        provider="bitbucket",
        instance_url=instance_url,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=_parse_scopes(scope_env),
        is_cloud=False,
        authorization_endpoint=authorize,
        token_endpoint=token,
        extra_authorize_params=None,
    )


def resolve_proxy_upstream() -> ProxyUpstream | None:
    """Resolve the full upstream spec for the single configured provider.

    Returns:
        A :class:`ProxyUpstream` when exactly one provider is configured with
        complete credentials, or ``None`` when the proxy is disabled or the
        configuration is incomplete.

    Raises:
        RuntimeError: If credentials for more than one provider family are set
            (see :func:`proxy_upstream_provider`).
    """
    provider = proxy_upstream_provider()
    if provider == "bitbucket":
        return _resolve_bitbucket_upstream()
    if provider == "atlassian":
        return _resolve_atlassian_upstream()
    return None
