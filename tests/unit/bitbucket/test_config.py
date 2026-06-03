"""Unit tests for BitbucketConfig."""

import os
from unittest.mock import patch

import pytest

from mcp_atlassian.bitbucket import BitbucketConfig
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig, OAuthConfig


class TestBitbucketConfigFromEnv:
    """Tests for BitbucketConfig.from_env()."""

    def test_from_env_dc_oauth_full(self):
        """Full DC OAuth client credentials build an OAuthConfig."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "BITBUCKET_OAUTH_CLIENT_ID": "client-id",
                "BITBUCKET_OAUTH_CLIENT_SECRET": "client-secret",
            },
            clear=True,
        ):
            config = BitbucketConfig.from_env()

        assert config.url == "https://bitbucket.corp.example.com"
        assert config.auth_type == "oauth"
        assert config.is_data_center is True
        assert config.is_cloud is False
        assert isinstance(config.oauth_config, OAuthConfig)
        assert config.oauth_config.client_id == "client-id"
        assert config.oauth_config.client_secret == "client-secret"
        # DC OAuth uses base_url, never cloud_id.
        assert config.oauth_config.base_url == "https://bitbucket.corp.example.com"
        assert config.oauth_config.cloud_id is None
        assert config.oauth_config.is_data_center is True

    def test_from_env_dc_byo_access_token(self):
        """A BYO access token builds a BYOAccessTokenOAuthConfig for DC."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "BITBUCKET_OAUTH_ACCESS_TOKEN": "byo-token",
            },
            clear=True,
        ):
            config = BitbucketConfig.from_env()

        assert config.auth_type == "oauth"
        assert isinstance(config.oauth_config, BYOAccessTokenOAuthConfig)
        assert config.oauth_config.access_token == "byo-token"
        assert config.oauth_config.base_url == "https://bitbucket.corp.example.com"
        assert config.oauth_config.is_data_center is True
        assert config.is_auth_configured() is True

    def test_from_env_minimal_oauth_enable(self):
        """ATLASSIAN_OAUTH_ENABLE builds a minimal config for header tokens."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "ATLASSIAN_OAUTH_ENABLE": "true",
            },
            clear=True,
        ):
            config = BitbucketConfig.from_env()

        assert config.auth_type == "oauth"
        assert isinstance(config.oauth_config, OAuthConfig)
        assert config.oauth_config.client_id == ""
        assert config.oauth_config.client_secret == ""
        assert config.oauth_config.base_url == "https://bitbucket.corp.example.com"
        # Minimal config is valid: the real token arrives per-request.
        assert config.is_auth_configured() is True

    def test_from_env_reads_proxy_and_ssl(self):
        """Proxy, SSL verify, and projects filter are read from env."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "BITBUCKET_OAUTH_ACCESS_TOKEN": "byo-token",
                "BITBUCKET_SSL_VERIFY": "false",
                "BITBUCKET_HTTPS_PROXY": "https://proxy.example.com:8443",
                "BITBUCKET_PROJECTS_FILTER": "PROJ,TEAM",
            },
            clear=True,
        ):
            config = BitbucketConfig.from_env()

        assert config.ssl_verify is False
        assert config.https_proxy == "https://proxy.example.com:8443"
        assert config.projects_filter == "PROJ,TEAM"

    def test_from_env_missing_url_raises(self):
        """Missing BITBUCKET_URL raises a ValueError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="BITBUCKET_URL"):
                BitbucketConfig.from_env()

    def test_from_env_missing_oauth_raises(self):
        """A URL without any OAuth config raises a ValueError."""
        with patch.dict(
            os.environ,
            {"BITBUCKET_URL": "https://bitbucket.corp.example.com"},
            clear=True,
        ):
            with pytest.raises(ValueError, match="authenticates via OAuth"):
                BitbucketConfig.from_env()

    def test_shared_atlassian_client_creds_do_not_satisfy_bitbucket(self):
        """Shared ATLASSIAN_OAUTH_* client creds must not configure Bitbucket.

        Bitbucket is a distinct OAuth provider on a distinct host; only
        service-scoped BITBUCKET_OAUTH_* (or ATLASSIAN_OAUTH_ENABLE) may satisfy
        it, matching the availability gate. Shared creds alone leave it
        unconfigured rather than borrowing another product's identity.
        """
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "ATLASSIAN_OAUTH_CLIENT_ID": "shared-id",
                "ATLASSIAN_OAUTH_CLIENT_SECRET": "shared-secret",
            },
            clear=True,
        ):
            with pytest.raises(ValueError, match="authenticates via OAuth"):
                BitbucketConfig.from_env()

    def test_shared_atlassian_access_token_does_not_satisfy_bitbucket(self):
        """A shared ATLASSIAN_OAUTH_ACCESS_TOKEN must not configure Bitbucket."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "ATLASSIAN_OAUTH_ACCESS_TOKEN": "shared-token",
            },
            clear=True,
        ):
            with pytest.raises(ValueError, match="authenticates via OAuth"):
                BitbucketConfig.from_env()

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
    def test_from_env_cloud_url_rejected(self, cloud_url):
        """DC-only: a Cloud URL raises a ValueError."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": cloud_url,
                "BITBUCKET_OAUTH_ACCESS_TOKEN": "byo-token",
            },
            clear=True,
        ):
            with pytest.raises(ValueError, match="Data Center"):
                BitbucketConfig.from_env()

    def test_from_env_timeout_parsed(self):
        """A valid BITBUCKET_TIMEOUT is parsed to an int."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "BITBUCKET_OAUTH_ACCESS_TOKEN": "byo-token",
                "BITBUCKET_TIMEOUT": "120",
            },
            clear=True,
        ):
            config = BitbucketConfig.from_env()

        assert config.timeout == 120

    def test_from_env_invalid_timeout_falls_back_with_warning(self, caplog):
        """A non-numeric BITBUCKET_TIMEOUT falls back to the default and warns."""
        with patch.dict(
            os.environ,
            {
                "BITBUCKET_URL": "https://bitbucket.corp.example.com",
                "BITBUCKET_OAUTH_ACCESS_TOKEN": "byo-token",
                "BITBUCKET_TIMEOUT": "not-a-number",
            },
            clear=True,
        ):
            config = BitbucketConfig.from_env()

        assert config.timeout == 75
        assert "Invalid BITBUCKET_TIMEOUT" in caplog.text


class TestBitbucketConfigIsAuthConfigured:
    """Tests for BitbucketConfig.is_auth_configured()."""

    def test_full_oauth_configured(self):
        """Full client credentials are considered configured."""
        config = BitbucketConfig(
            url="https://bitbucket.corp.example.com",
            auth_type="oauth",
            oauth_config=OAuthConfig(
                client_id="c",
                client_secret="s",
                redirect_uri="r",
                scope="WRITE",
                base_url="https://bitbucket.corp.example.com",
            ),
        )
        assert config.is_auth_configured() is True

    def test_no_oauth_config_not_configured(self):
        """A config with no oauth_config is not configured."""
        config = BitbucketConfig(
            url="https://bitbucket.corp.example.com",
            auth_type="oauth",
            oauth_config=None,
        )
        assert config.is_auth_configured() is False
