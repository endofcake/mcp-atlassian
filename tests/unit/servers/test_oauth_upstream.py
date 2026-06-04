"""Unit tests for OAuth proxy upstream provider resolution."""

from __future__ import annotations

import pytest

from mcp_atlassian.servers.oauth_upstream import (
    proxy_upstream_provider,
    resolve_proxy_upstream,
)

# Every env var the resolver reads, cleared to a known baseline per test so
# ambient environment cannot leak a second provider into the detection.
_ALL_UPSTREAM_ENV = (
    "ATLASSIAN_OAUTH_PROXY_ENABLE",
    "ATLASSIAN_OAUTH_INSTANCE_URL",
    "ATLASSIAN_OAUTH_CLIENT_ID",
    "ATLASSIAN_OAUTH_CLIENT_SECRET",
    "ATLASSIAN_OAUTH_REDIRECT_URI",
    "ATLASSIAN_OAUTH_SCOPE",
    "JIRA_URL",
    "JIRA_OAUTH_CLIENT_ID",
    "JIRA_OAUTH_CLIENT_SECRET",
    "CONFLUENCE_URL",
    "CONFLUENCE_OAUTH_CLIENT_ID",
    "CONFLUENCE_OAUTH_CLIENT_SECRET",
    "BITBUCKET_URL",
    "BITBUCKET_OAUTH_CLIENT_ID",
    "BITBUCKET_OAUTH_CLIENT_SECRET",
    "BITBUCKET_OAUTH_REDIRECT_URI",
    "BITBUCKET_OAUTH_SCOPE",
)


@pytest.fixture(autouse=True)
def _clear_upstream_env(monkeypatch):
    for name in _ALL_UPSTREAM_ENV:
        monkeypatch.delenv(name, raising=False)


class TestProxyUpstreamProvider:
    """proxy_upstream_provider identifies the single configured provider."""

    def test_none_when_proxy_disabled(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_ID", "id")
        # Proxy enable flag unset → no upstream regardless of credentials.
        assert proxy_upstream_provider() is None

    def test_none_when_no_client_credentials(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        assert proxy_upstream_provider() is None

    def test_atlassian_when_shared_client_id_set(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_ID", "id")
        assert proxy_upstream_provider() == "atlassian"

    def test_atlassian_when_service_specific_client_id_set(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("JIRA_OAUTH_CLIENT_ID", "jira-id")
        assert proxy_upstream_provider() == "atlassian"

    def test_bitbucket_when_only_bitbucket_client_id_set(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("BITBUCKET_OAUTH_CLIENT_ID", "bb-id")
        assert proxy_upstream_provider() == "bitbucket"

    def test_raises_when_both_families_configured(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("JIRA_OAUTH_CLIENT_ID", "jira-id")
        monkeypatch.setenv("BITBUCKET_OAUTH_CLIENT_ID", "bb-id")
        with pytest.raises(RuntimeError, match="more than one upstream provider"):
            proxy_upstream_provider()

    def test_no_raise_when_both_configured_but_proxy_disabled(self, monkeypatch):
        # The overlap only matters when the proxy is enabled; a disabled proxy
        # fronts nobody, so configured-but-unused credentials are not ambiguous.
        monkeypatch.setenv("JIRA_OAUTH_CLIENT_ID", "jira-id")
        monkeypatch.setenv("BITBUCKET_OAUTH_CLIENT_ID", "bb-id")
        assert proxy_upstream_provider() is None


class TestResolveAtlassianUpstream:
    """resolve_proxy_upstream preserves the Atlassian endpoint behaviour."""

    def _set_atlassian_env(self, monkeypatch, *, instance_url: str) -> None:
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv(
            "ATLASSIAN_OAUTH_REDIRECT_URI", "http://localhost:3000/callback"
        )
        monkeypatch.setenv("JIRA_URL", instance_url)

    def test_data_center_endpoints(self, monkeypatch):
        self._set_atlassian_env(monkeypatch, instance_url="https://jira.example.com")
        monkeypatch.setenv("ATLASSIAN_OAUTH_SCOPE", "read:jira-work")

        upstream = resolve_proxy_upstream()

        assert upstream is not None
        assert upstream.provider == "atlassian"
        assert upstream.is_cloud is False
        assert (
            upstream.authorization_endpoint
            == "https://jira.example.com/rest/oauth2/latest/authorize"
        )
        assert (
            upstream.token_endpoint
            == "https://jira.example.com/rest/oauth2/latest/token"
        )
        assert upstream.scopes == ["read:jira-work"]
        assert upstream.extra_authorize_params is None

    def test_cloud_endpoints_and_extra_params(self, monkeypatch):
        self._set_atlassian_env(monkeypatch, instance_url="https://acme.atlassian.net")

        upstream = resolve_proxy_upstream()

        assert upstream is not None
        assert upstream.is_cloud is True
        assert upstream.authorization_endpoint.startswith("https://auth.atlassian.com")
        assert upstream.extra_authorize_params == {
            "audience": "api.atlassian.com",
            "prompt": "consent",
        }

    def test_none_when_secret_missing(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("ATLASSIAN_OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv(
            "ATLASSIAN_OAUTH_REDIRECT_URI", "http://localhost:3000/callback"
        )
        monkeypatch.setenv("JIRA_URL", "https://jira.example.com")

        assert resolve_proxy_upstream() is None


class TestResolveBitbucketUpstream:
    """resolve_proxy_upstream resolves a Bitbucket Data Center upstream."""

    def _set_bitbucket_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.example.com")
        monkeypatch.setenv("BITBUCKET_OAUTH_CLIENT_ID", "bb-client-id")
        monkeypatch.setenv("BITBUCKET_OAUTH_CLIENT_SECRET", "bb-client-secret")
        monkeypatch.setenv(
            "BITBUCKET_OAUTH_REDIRECT_URI",
            "https://mcp.example.com/mcp-atlassian/callback",
        )

    def test_data_center_endpoints_and_scope(self, monkeypatch):
        self._set_bitbucket_env(monkeypatch)
        monkeypatch.setenv("BITBUCKET_OAUTH_SCOPE", "REPO_READ")

        upstream = resolve_proxy_upstream()

        assert upstream is not None
        assert upstream.provider == "bitbucket"
        assert upstream.is_cloud is False
        assert (
            upstream.authorization_endpoint
            == "https://bitbucket.example.com/rest/oauth2/latest/authorize"
        )
        assert (
            upstream.token_endpoint
            == "https://bitbucket.example.com/rest/oauth2/latest/token"
        )
        assert upstream.scopes == ["REPO_READ"]
        # Bitbucket Data Center is never Cloud: no Cloud audience/prompt params.
        assert upstream.extra_authorize_params is None

    def test_redirect_uri_falls_back_to_shared_atlassian_var(self, monkeypatch):
        self._set_bitbucket_env(monkeypatch)
        monkeypatch.delenv("BITBUCKET_OAUTH_REDIRECT_URI", raising=False)
        monkeypatch.setenv(
            "ATLASSIAN_OAUTH_REDIRECT_URI", "https://mcp.example.com/shared/callback"
        )

        upstream = resolve_proxy_upstream()

        assert upstream is not None
        assert upstream.redirect_uri == "https://mcp.example.com/shared/callback"

    def test_scope_has_no_atlassian_fallback(self, monkeypatch):
        # A scope string meant for a different provider must not leak in.
        self._set_bitbucket_env(monkeypatch)
        monkeypatch.delenv("BITBUCKET_OAUTH_SCOPE", raising=False)
        monkeypatch.setenv("ATLASSIAN_OAUTH_SCOPE", "read:jira-work")

        upstream = resolve_proxy_upstream()

        assert upstream is not None
        assert upstream.scopes == []

    def test_none_when_secret_missing(self, monkeypatch):
        self._set_bitbucket_env(monkeypatch)
        monkeypatch.delenv("BITBUCKET_OAUTH_CLIENT_SECRET", raising=False)

        assert resolve_proxy_upstream() is None

    @pytest.mark.parametrize(
        "cloud_url",
        [
            # The host a Bitbucket user would actually type, which the
            # Atlassian Cloud check does not match.
            "https://bitbucket.org",
            "https://api.bitbucket.org",
            # Atlassian Cloud host: nonsensical for BITBUCKET_URL, still rejected.
            "https://mysite.atlassian.net",
        ],
    )
    def test_cloud_url_rejected(self, monkeypatch, cloud_url):
        # Data Center only: a Cloud-looking BITBUCKET_URL must not build a
        # broken Data Center-shaped upstream on a Cloud host.
        self._set_bitbucket_env(monkeypatch)
        monkeypatch.setenv("BITBUCKET_URL", cloud_url)

        assert resolve_proxy_upstream() is None

    def test_none_when_redirect_uri_absent_with_no_fallback(self, monkeypatch):
        self._set_bitbucket_env(monkeypatch)
        monkeypatch.delenv("BITBUCKET_OAUTH_REDIRECT_URI", raising=False)
        monkeypatch.delenv("ATLASSIAN_OAUTH_REDIRECT_URI", raising=False)

        assert resolve_proxy_upstream() is None

    def test_byo_access_token_only_is_not_a_proxy_upstream(self, monkeypatch):
        # A bring-your-own access token (no OAuth client credentials) does not
        # drive the proxy; the proxy needs client credentials for the DCR flow.
        monkeypatch.setenv("ATLASSIAN_OAUTH_PROXY_ENABLE", "true")
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.example.com")
        monkeypatch.setenv("BITBUCKET_OAUTH_ACCESS_TOKEN", "byo-token")

        assert proxy_upstream_provider() is None
        assert resolve_proxy_upstream() is None
