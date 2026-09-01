"""Helm chart tests for the Bitbucket Data Center env contract.

These guard against env-contract drift: the chart must wire exactly the
``BITBUCKET_*`` environment variables the server code reads, or a Helm-only
deploy silently degrades. The render tests shell out to ``helm`` when it is on
PATH and skip otherwise; the contract tests parse the templates as text and
run unconditionally.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHART_DIR = _REPO_ROOT / "helm"
_DEPLOYMENT_TEMPLATE = _CHART_DIR / "templates" / "deployment.yaml"
_CONFIGMAP_TEMPLATE = _CHART_DIR / "templates" / "configmap.yaml"

# The env vars an oauth-mode Bitbucket deploy must provide. These mirror what
# BitbucketConfig.from_env and the OAuth proxy upstream resolver read; omitting
# any of them degrades the deploy (e.g. a missing redirect URI drops the proxy
# back to bearer-forwarding).
_BITBUCKET_OAUTH_ENV = {
    "BITBUCKET_URL",
    "BITBUCKET_OAUTH_CLIENT_ID",
    "BITBUCKET_OAUTH_CLIENT_SECRET",
    "BITBUCKET_OAUTH_REDIRECT_URI",
    "BITBUCKET_OAUTH_SCOPE",
}

_HELM = shutil.which("helm")
requires_helm = pytest.mark.skipif(_HELM is None, reason="helm binary not on PATH")


def _helm_template(
    values: dict, tmp_path: Path, show_only: str = "templates/deployment.yaml"
) -> subprocess.CompletedProcess[str]:
    """Render one chart template with the given values via ``helm template``."""
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump(values))
    assert _HELM is not None  # guarded by requires_helm
    return subprocess.run(
        [
            _HELM,
            "template",
            "rel",
            str(_CHART_DIR),
            "-f",
            str(values_file),
            "--show-only",
            show_only,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _container_env(rendered_deployment: str) -> dict[str, dict]:
    """Map env-var name -> its entry from a rendered Deployment manifest."""
    doc = yaml.safe_load(rendered_deployment)
    container = doc["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry for entry in container.get("env", [])}


def _oauth_values(**bitbucket_overrides: object) -> dict:
    """Build chart values with Bitbucket enabled in oauth mode."""
    bitbucket = {
        "enabled": True,
        "url": "https://bitbucket.corp.example.com",
        "authMode": "oauth",
        "oauthClientId": "bb-id",
        "oauthClientSecret": "bb-secret",
    }
    bitbucket.update(bitbucket_overrides)
    return {"authMode": "api-token", "bitbucket": bitbucket}


class TestBitbucketHelmContract:
    """The chart wires the BITBUCKET_* env contract the server code reads."""

    def test_deployment_declares_bitbucket_oauth_env(self):
        """Every oauth-deploy env var is referenced in the deployment template."""
        template = _DEPLOYMENT_TEMPLATE.read_text()
        missing = {name for name in _BITBUCKET_OAUTH_ENV if name not in template}
        assert not missing, f"deployment.yaml is missing env wiring for {missing}"

    def test_configmap_declares_oauth_redirect_and_scope_keys(self):
        """The non-secret oauth keys the deployment references are declared."""
        configmap = _CONFIGMAP_TEMPLATE.read_text()
        deployment = _DEPLOYMENT_TEMPLATE.read_text()
        for key in ("bitbucket-oauth-redirect-uri", "bitbucket-oauth-scope"):
            assert key in configmap, f"configmap.yaml does not declare {key}"
            assert key in deployment, f"deployment.yaml does not reference {key}"

    @requires_helm
    def test_render_wires_full_oauth_env_set(self, tmp_path):
        """An oauth-mode render exposes the full Bitbucket env set."""
        result = _helm_template(
            _oauth_values(
                oauthRedirectUri="https://mcp.example.com/callback",
                oauthScope="REPO_READ",
            ),
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        env = _container_env(result.stdout)
        assert _BITBUCKET_OAUTH_ENV <= set(env)
        # The non-secret vars resolve from the ConfigMap.
        for name in ("BITBUCKET_OAUTH_REDIRECT_URI", "BITBUCKET_OAUTH_SCOPE"):
            assert "configMapKeyRef" in env[name]["valueFrom"]

    @requires_helm
    def test_render_byot_wires_access_token_from_secret(self, tmp_path):
        """A byot-mode render exposes the token from the Secret and no client env."""
        result = _helm_template(
            _oauth_values(authMode="byot", oauthAccessToken="bb-token"),
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        env = _container_env(result.stdout)
        assert "BITBUCKET_OAUTH_ACCESS_TOKEN" in env
        assert "secretKeyRef" in env["BITBUCKET_OAUTH_ACCESS_TOKEN"]["valueFrom"]
        assert "BITBUCKET_OAUTH_CLIENT_ID" not in env

    @requires_helm
    def test_render_passthrough_headers_resolve_from_configmap(self, tmp_path):
        """passthroughHeaders renders BITBUCKET_PASSTHROUGH_HEADERS via ConfigMap."""
        result = _helm_template(
            _oauth_values(passthroughHeaders="X-Forwarded-User"), tmp_path
        )
        assert result.returncode == 0, result.stderr
        env = _container_env(result.stdout)
        assert "BITBUCKET_PASSTHROUGH_HEADERS" in env
        assert "configMapKeyRef" in env["BITBUCKET_PASSTHROUGH_HEADERS"]["valueFrom"]

    @requires_helm
    def test_render_custom_headers_resolve_from_secret(self, tmp_path):
        """customHeaders renders BITBUCKET_CUSTOM_HEADERS via the Secret.

        Custom headers can carry auth material, so they must resolve from the
        Secret.
        """
        result = _helm_template(
            _oauth_values(customHeaders="X-Custom=1,X-Other=2"), tmp_path
        )
        assert result.returncode == 0, result.stderr
        env = _container_env(result.stdout)
        assert "BITBUCKET_CUSTOM_HEADERS" in env
        assert "secretKeyRef" in env["BITBUCKET_CUSTOM_HEADERS"]["valueFrom"]

    @requires_helm
    def test_render_proxy_bitbucket_static_values_are_quoted(self, tmp_path):
        """Static per-service proxy values render as quoted strings.

        A proxy URL containing YAML-significant characters (e.g. a ``#`` in a
        password) must survive rendering intact rather than being truncated as
        a comment.
        """
        values = _oauth_values()
        values["proxy"] = {
            "enabled": True,
            "bitbucket": {"http": "http://user:p#ss@proxy.example.com:8080"},
        }
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr
        env = _container_env(result.stdout)
        value = env["BITBUCKET_HTTP_PROXY"]["value"]
        assert value == "http://user:p#ss@proxy.example.com:8080"

    @requires_helm
    def test_render_proxy_bitbucket_wpad_override(self, tmp_path):
        """The proxy.bitbucket block feeds the BITBUCKET_PROXY_WPAD_* env vars."""
        values = _oauth_values()
        values["proxy"] = {
            "enabled": True,
            "bitbucket": {
                "wpad": {"enabled": True, "url": "http://wpad.corp/wpad.dat"}
            },
        }
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr
        env = _container_env(result.stdout)
        assert env["BITBUCKET_PROXY_WPAD_ENABLE"]["value"] == "true"
        assert env["BITBUCKET_PROXY_WPAD_URL"]["value"] == "http://wpad.corp/wpad.dat"

    @requires_helm
    def test_render_ssl_verify_opt_out_is_reachable(self, tmp_path):
        """A YAML boolean ``false`` still emits BITBUCKET_SSL_VERIFY=false.

        Regression guard for the truthiness gate: ``sslVerify: false`` (boolean,
        not the string "false") must not drop the env var, or the verification
        opt-out is unreachable.
        """
        result = _helm_template(_oauth_values(sslVerify=False), tmp_path)
        assert result.returncode == 0, result.stderr
        env = _container_env(result.stdout)
        assert "BITBUCKET_SSL_VERIFY" in env
        assert env["BITBUCKET_SSL_VERIFY"]["value"] == "false"

    @requires_helm
    def test_render_invalid_authmode_fails_loudly(self, tmp_path):
        """A typo'd authMode aborts the render instead of silently disabling."""
        result = _helm_template(_oauth_values(authMode="ouath"), tmp_path)
        assert result.returncode != 0
        assert "bitbucket.authMode" in result.stderr

    @requires_helm
    @pytest.mark.parametrize(
        "show_only",
        [
            "templates/configmap.yaml",
            "templates/secret.yaml",
            "templates/deployment.yaml",
        ],
    )
    def test_render_partial_render_validates_bitbucket_block(self, tmp_path, show_only):
        """A single-template render fails on the same misconfiguration.

        The validation lives in a named template that every manifest reading
        the bitbucket block includes, so ``--show-only`` of any one of them
        reports the error rather than depending on another template to do so.
        """
        values = _oauth_values()
        del values["bitbucket"]["url"]
        result = _helm_template(values, tmp_path, show_only=show_only)
        assert result.returncode != 0
        assert "bitbucket.url is required" in result.stderr

    @requires_helm
    def test_render_disabled_bitbucket_ignores_authmode(self, tmp_path):
        """The guards only fire when Bitbucket is enabled."""
        values = {
            "authMode": "api-token",
            "bitbucket": {"enabled": False, "authMode": "nonsense"},
        }
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr

    @requires_helm
    def test_render_enabled_without_url_fails_loudly(self, tmp_path):
        """Enabling Bitbucket without a url aborts the render instead of crashlooping."""
        values = _oauth_values()
        del values["bitbucket"]["url"]
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "bitbucket.url is required" in result.stderr

    @requires_helm
    @pytest.mark.parametrize(
        "missing_key",
        ["oauthClientId", "oauthClientSecret"],
    )
    def test_render_oauth_without_client_credentials_fails_loudly(
        self, tmp_path, missing_key
    ):
        """oauth mode without client credentials aborts the render."""
        result = _helm_template(_oauth_values(**{missing_key: ""}), tmp_path)
        assert result.returncode != 0
        assert f"bitbucket.{missing_key} is required" in result.stderr

    @requires_helm
    def test_render_oauth_with_empty_scope_fails_loudly(self, tmp_path):
        """oauth mode with an explicitly empty scope aborts the render.

        Bitbucket Data Center has no default OAuth scope; the server refuses to
        start without one, so the chart surfaces the mistake at render time.
        """
        result = _helm_template(_oauth_values(oauthScope=""), tmp_path)
        assert result.returncode != 0
        assert "bitbucket.oauthScope is required" in result.stderr

    @requires_helm
    def test_render_byot_without_token_fails_loudly(self, tmp_path):
        """byot mode without an access token aborts the render."""
        result = _helm_template(_oauth_values(authMode="byot"), tmp_path)
        assert result.returncode != 0
        assert "bitbucket.oauthAccessToken is required" in result.stderr

    @requires_helm
    def test_render_whitespace_url_fails_loudly(self, tmp_path):
        """A whitespace-only url is rejected like a missing one."""
        result = _helm_template(_oauth_values(url="   "), tmp_path)
        assert result.returncode != 0
        assert "bitbucket.url is required" in result.stderr

    @requires_helm
    def test_render_trims_surrounding_whitespace_from_url(self, tmp_path):
        """Surrounding whitespace in the url is dropped from the ConfigMap.

        The url check already ignores surrounding whitespace, so the rendered
        value must match what the check accepted instead of handing the
        server a base url with leading or trailing spaces.
        """
        result = _helm_template(
            _oauth_values(url="  https://bitbucket.corp.example.com  "),
            tmp_path,
            show_only="templates/configmap.yaml",
        )
        assert result.returncode == 0, result.stderr
        data = yaml.safe_load(result.stdout)["data"]
        assert data["bitbucket-url"] == "https://bitbucket.corp.example.com"

    @requires_helm
    def test_render_trims_surrounding_whitespace_from_unused_jira_url(self, tmp_path):
        """A whitespace-only Jira url renders as an empty JIRA_URL.

        The provider-family guard treats such a url as unused, so the ConfigMap
        must not ship a value the server would read as a configured Jira.
        """
        values = {
            "authMode": "api-token",
            "bitbucket": {"enabled": False},
            "jira": {"enabled": True, "url": "   "},
        }
        result = _helm_template(values, tmp_path, show_only="templates/configmap.yaml")
        assert result.returncode == 0, result.stderr
        assert yaml.safe_load(result.stdout)["data"]["jira-url"] == ""

    @requires_helm
    def test_render_trims_surrounding_whitespace_from_credentials(self, tmp_path):
        """Surrounding whitespace in the client id is dropped from the Secret.

        The credential check ignores surrounding whitespace, so the emitted
        value must match what the check accepted.
        """
        result = _helm_template(
            _oauth_values(oauthClientId="  bb-id  "),
            tmp_path,
            show_only="templates/secret.yaml",
        )
        assert result.returncode == 0, result.stderr
        data = yaml.safe_load(result.stdout)["stringData"]
        assert data["bitbucket-oauth-client-id"] == "bb-id"

    @requires_helm
    @pytest.mark.parametrize(
        ("overrides", "manifest", "key"),
        [
            ({"url": 12345}, "configmap", "bitbucket-url"),
            ({"oauthClientId": 12345}, "secret", "bitbucket-oauth-client-id"),
            ({"oauthClientSecret": 12345}, "secret", "bitbucket-oauth-client-secret"),
            ({"oauthScope": 12345}, "configmap", "bitbucket-oauth-scope"),
            (
                {"authMode": "byot", "oauthAccessToken": 12345},
                "secret",
                "bitbucket-oauth-access-token",
            ),
        ],
    )
    def test_render_numeric_value_renders_as_string(
        self, tmp_path, overrides, manifest, key
    ):
        """A numeric value in any guarded field renders as a quoted string.

        YAML hands the template a number where a string is expected; every
        guard coerces before trimming, so the render neither dies with a Go
        template type error nor drops the value.
        """
        result = _helm_template(
            _oauth_values(**overrides),
            tmp_path,
            show_only=f"templates/{manifest}.yaml",
        )
        assert result.returncode == 0, result.stderr
        section = "data" if manifest == "configmap" else "stringData"
        assert yaml.safe_load(result.stdout)[section][key] == "12345"

    @requires_helm
    @pytest.mark.parametrize(
        ("overrides", "key", "kind"),
        [
            ({"oauthScope": ["REPO_READ", "REPO_WRITE"]}, "oauthScope", "slice"),
            ({"url": {"host": "bitbucket.corp.example.com"}}, "url", "map"),
        ],
    )
    def test_render_non_scalar_value_fails_loudly(self, tmp_path, overrides, key, kind):
        """A list or map in a guarded field aborts with a message naming it.

        Coercing such a value would render its printed form, for example
        ``[REPO_READ REPO_WRITE]`` as the scope, which the server would then
        send upstream.
        """
        result = _helm_template(_oauth_values(**overrides), tmp_path)
        assert result.returncode != 0
        assert f"bitbucket.{key} must be a string, got a {kind}" in result.stderr

    @requires_helm
    def test_render_numeric_authmode_is_quoted_in_error(self, tmp_path):
        """A numeric authMode is reported as its string form."""
        result = _helm_template(_oauth_values(authMode=1), tmp_path)
        assert result.returncode != 0
        assert 'got "1"' in result.stderr

    @requires_helm
    @pytest.mark.parametrize("zero_key", ["url", "oauthScope"])
    def test_render_zero_value_reaches_descriptive_error(self, tmp_path, zero_key):
        """A falsy number reaches the chart's own error rather than a type error."""
        result = _helm_template(_oauth_values(**{zero_key: 0}), tmp_path)
        assert result.returncode != 0
        assert f"bitbucket.{zero_key} is required" in result.stderr

    @requires_helm
    @pytest.mark.parametrize("blank_key", ["oauthClientId", "oauthClientSecret"])
    def test_render_oauth_with_whitespace_credentials_fails_loudly(
        self, tmp_path, blank_key
    ):
        """A whitespace-only client id or secret is rejected like a missing one.

        A value of spaces passes a truthiness check, and the pod then fails to
        authenticate; the chart surfaces the mistake at render time instead.
        """
        result = _helm_template(_oauth_values(**{blank_key: "   "}), tmp_path)
        assert result.returncode != 0
        assert f"bitbucket.{blank_key} is required" in result.stderr

    @requires_helm
    def test_render_byot_with_whitespace_token_fails_loudly(self, tmp_path):
        """A whitespace-only byot access token is rejected like a missing one."""
        result = _helm_template(
            _oauth_values(authMode="byot", oauthAccessToken="   "), tmp_path
        )
        assert result.returncode != 0
        assert "bitbucket.oauthAccessToken is required" in result.stderr

    @requires_helm
    @pytest.mark.parametrize(
        "top_level_auth_mode",
        ["oauth", "byot", "api-token", "personal-token", "external", " OAuth "],
    )
    def test_render_proxy_with_configured_jira_fails_loudly(
        self, tmp_path, top_level_auth_mode
    ):
        """Bitbucket behind the proxy alongside a configured Jira aborts.

        The proxy fronts exactly one provider family. Whatever the top-level
        authMode says, the server rejects proxy-minted tokens for Jira (and
        refuses to start at all under ``external``), so the chart fails the
        render rather than shipping a pod whose Jira tools cannot authenticate.
        """
        values = _oauth_values()
        values["authMode"] = top_level_auth_mode
        values["jira"] = {"enabled": True, "url": "https://jira.corp.example.com"}
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "one provider family" in result.stderr
        assert "configured jira" in result.stderr

    @requires_helm
    def test_render_proxy_with_numeric_jira_url_fails_loudly(self, tmp_path):
        """A non-string Jira url still reaches the guard's own error.

        YAML can hand the template a number where a string is expected; the
        guard must still report the provider-family conflict.
        """
        values = _oauth_values()
        values["jira"] = {"enabled": True, "url": 12345}
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "one provider family" in result.stderr

    @requires_helm
    def test_render_proxy_with_configured_confluence_fails_loudly(self, tmp_path):
        """A configured Confluence trips the guard the same way Jira does."""
        values = _oauth_values()
        values["confluence"] = {
            "enabled": True,
            "url": "https://confluence.corp.example.com",
        }
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "one provider family" in result.stderr
        assert "configured confluence" in result.stderr

    @requires_helm
    def test_render_proxy_names_every_configured_family(self, tmp_path):
        """With both Jira and Confluence configured the error names both."""
        values = _oauth_values()
        values["jira"] = {"enabled": True, "url": "https://jira.corp.example.com"}
        values["confluence"] = {
            "enabled": True,
            "url": "https://confluence.corp.example.com",
        }
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "configured jira and confluence" in result.stderr

    @requires_helm
    def test_render_byot_bitbucket_with_configured_jira_fails_loudly(self, tmp_path):
        """Bitbucket byot alongside a configured Jira under the proxy aborts.

        The Bitbucket auth mode makes no difference: whichever family the
        proxy fronts, the other family's tools are unusable.
        """
        values = _oauth_values(authMode="byot", oauthAccessToken="bb-token")
        values["jira"] = {"enabled": True, "url": "https://jira.corp.example.com"}
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "one provider family" in result.stderr

    @requires_helm
    def test_render_bitbucket_only_release_ignores_stale_authmode(self, tmp_path):
        """A Bitbucket-only release renders despite a stale top-level authMode.

        With Jira and Confluence disabled there is no second provider family,
        so a leftover top-level ``authMode: oauth`` and a leftover Jira url
        must not fail the render.
        """
        values = _oauth_values(
            oauthRedirectUri="https://mcp.example.com/callback",
            oauthScope="REPO_READ",
        )
        values["authMode"] = "oauth"
        values["oauthProxy"] = {"enabled": True}
        values["jira"] = {"enabled": False, "url": "https://jira.corp.example.com"}
        values["confluence"] = {"enabled": False}
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr

    @requires_helm
    def test_render_proxy_with_byot_bitbucket_fails_loudly(self, tmp_path):
        """Bitbucket byot behind the proxy aborts the render.

        The proxy is built from Bitbucket OAuth client credentials; under
        ``byot`` there are none, so the server would log a warning and start
        with the proxy disabled.
        """
        values = _oauth_values(authMode="byot", oauthAccessToken="bb-token")
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert 'bitbucket.authMode must be "oauth" when oauthProxy' in result.stderr

    @requires_helm
    @pytest.mark.parametrize("redirect_uri", ["", "   "])
    def test_render_proxy_without_redirect_uri_fails_loudly(
        self, tmp_path, redirect_uri
    ):
        """An empty or whitespace redirect URI behind the proxy aborts.

        An empty value makes the server start with the proxy disabled; a
        value of spaces passes the server's truthiness check and registers an
        unusable redirect with Bitbucket.
        """
        values = _oauth_values(oauthRedirectUri=redirect_uri)
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "bitbucket.oauthRedirectUri is required" in result.stderr

    @requires_helm
    def test_render_proxy_with_top_level_client_id_fails_loudly(self, tmp_path):
        """A top-level oauth.clientId next to the Bitbucket proxy aborts.

        Both provider families would supply proxy credentials, and the server
        refuses to start rather than pick one.
        """
        values = _oauth_values()
        values["authMode"] = "oauth"
        values["oauth"] = {"clientId": "cloud-id", "clientSecret": "cloud-secret"}
        values["jira"] = {"enabled": False}
        values["confluence"] = {"enabled": False}
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "oauth.clientId cannot be set" in result.stderr

    @requires_helm
    def test_render_proxy_with_whitespace_top_level_client_id_fails_loudly(
        self, tmp_path
    ):
        """A whitespace top-level oauth.clientId is rejected like a set one.

        The Secret emits the value untrimmed, and the server treats a value
        of spaces as a configured client id.
        """
        values = _oauth_values()
        values["authMode"] = "oauth"
        values["oauth"] = {"clientId": "   "}
        values["jira"] = {"enabled": False}
        values["confluence"] = {"enabled": False}
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "oauth.clientId cannot be set" in result.stderr

    @requires_helm
    def test_render_proxy_ignores_top_level_client_id_outside_oauth_mode(
        self, tmp_path
    ):
        """A leftover oauth.clientId under another top-level authMode is fine.

        The chart emits ATLASSIAN_OAUTH_CLIENT_ID only when the top-level
        authMode is ``oauth``, so the server never sees the leftover value.
        """
        values = _oauth_values()
        values["authMode"] = "api-token"
        values["oauth"] = {"clientId": "leftover"}
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "ATLASSIAN_OAUTH_CLIENT_ID" not in result.stdout

    @requires_helm
    def test_render_null_redirect_uri_without_proxy_emits_empty(self, tmp_path):
        """A null redirect URI renders as an empty string, not ``<nil>``."""
        result = _helm_template(
            _oauth_values(oauthRedirectUri=None),
            tmp_path,
            show_only="templates/configmap.yaml",
        )
        assert result.returncode == 0, result.stderr
        data = yaml.safe_load(result.stdout)["data"]
        assert data["bitbucket-oauth-redirect-uri"] == ""

    @requires_helm
    def test_render_byot_without_proxy_is_allowed(self, tmp_path):
        """Without the proxy, byot needs no client credentials or redirect."""
        values = _oauth_values(
            authMode="byot", oauthAccessToken="bb-token", oauthRedirectUri=""
        )
        values["oauthProxy"] = {"enabled": False}
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr

    @requires_helm
    def test_render_trims_surrounding_whitespace_from_redirect_and_scope(
        self, tmp_path
    ):
        """Padded redirect URI and scope reach the ConfigMap trimmed.

        Bitbucket matches the redirect URI exactly against the application
        link, so a value with surrounding spaces fails at first login.
        """
        result = _helm_template(
            _oauth_values(
                oauthRedirectUri="  https://mcp.example.com/callback  ",
                oauthScope="  REPO_READ  ",
            ),
            tmp_path,
            show_only="templates/configmap.yaml",
        )
        assert result.returncode == 0, result.stderr
        data = yaml.safe_load(result.stdout)["data"]
        assert (
            data["bitbucket-oauth-redirect-uri"] == "https://mcp.example.com/callback"
        )
        assert data["bitbucket-oauth-scope"] == "REPO_READ"

    @requires_helm
    @pytest.mark.parametrize("unused_url", ["", "   "])
    def test_render_proxy_with_unused_default_families_is_allowed(
        self, tmp_path, unused_url
    ):
        """A proxy fronting only Bitbucket renders with the default Jira block.

        Jira and Confluence default to enabled with an empty url. That block is
        unused, so it must not trip the guard; a whitespace-only url counts as
        empty too.
        """
        values = _oauth_values(
            oauthRedirectUri="https://mcp.example.com/callback",
            oauthScope="REPO_READ",
        )
        values["jira"] = {"enabled": True, "url": unused_url}
        values["confluence"] = {"enabled": True, "url": unused_url}
        values["oauthProxy"] = {"enabled": True}
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr

    @requires_helm
    def test_render_cross_family_without_proxy_is_allowed(self, tmp_path):
        """Without the OAuth proxy, both families may be configured together."""
        values = _oauth_values()
        values["authMode"] = "oauth"
        values["oauth"] = {"clientId": "cloud-id", "clientSecret": "cloud-secret"}
        values["jira"] = {"enabled": True, "url": "https://jira.corp.example.com"}
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr

    @requires_helm
    def test_render_persists_tokens_for_bitbucket_oauth(self, tmp_path):
        """Bitbucket oauth mode gets the token-cache mount and PVC.

        The persistence guards used to read only the top-level authMode, so a
        Bitbucket-only OAuth release with persistence enabled rendered neither
        a PVC nor a mount.
        """
        values = _oauth_values(
            oauthRedirectUri="https://mcp.example.com/callback",
            oauthScope="REPO_READ",
        )
        values["authMode"] = "api-token"
        values["persistence"] = {"enabled": True}

        deployment = _helm_template(values, tmp_path)
        assert deployment.returncode == 0, deployment.stderr
        doc = yaml.safe_load(deployment.stdout)
        pod = doc["spec"]["template"]["spec"]
        mounts = pod["containers"][0]["volumeMounts"]
        assert any(m["name"] == "oauth-tokens" for m in mounts)
        volume = next(v for v in pod["volumes"] if v["name"] == "oauth-tokens")
        assert volume["persistentVolumeClaim"]["claimName"] == "rel-mcp-atlassian"

        pvc = _helm_template(values, tmp_path, show_only="templates/pvc.yaml")
        assert pvc.returncode == 0, pvc.stderr
        assert yaml.safe_load(pvc.stdout)["kind"] == "PersistentVolumeClaim"

    @requires_helm
    def test_render_persists_tokens_into_existing_claim(self, tmp_path):
        """An existing claim is mounted for Bitbucket oauth and no PVC is made."""
        values = _oauth_values(
            oauthRedirectUri="https://mcp.example.com/callback",
            oauthScope="REPO_READ",
        )
        values["authMode"] = "api-token"
        values["persistence"] = {"enabled": True, "existingClaim": "my-claim"}

        deployment = _helm_template(values, tmp_path)
        assert deployment.returncode == 0, deployment.stderr
        pod = yaml.safe_load(deployment.stdout)["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "oauth-tokens")
        assert volume["persistentVolumeClaim"]["claimName"] == "my-claim"

        pvc = _helm_template(values, tmp_path, show_only="templates/pvc.yaml")
        assert pvc.returncode != 0 or "PersistentVolumeClaim" not in pvc.stdout

    @requires_helm
    def test_render_skips_token_persistence_for_bitbucket_byot(self, tmp_path):
        """A byot token has nothing to cache, so no mount or PVC is rendered."""
        values = {
            "authMode": "api-token",
            "persistence": {"enabled": True},
            "bitbucket": {
                "enabled": True,
                "url": "https://bitbucket.corp.example.com",
                "authMode": "byot",
                "oauthAccessToken": "bb-token",
            },
        }
        deployment = _helm_template(values, tmp_path)
        assert deployment.returncode == 0, deployment.stderr
        pod = yaml.safe_load(deployment.stdout)["spec"]["template"]["spec"]
        assert not any(v["name"] == "oauth-tokens" for v in pod.get("volumes") or [])

        pvc = _helm_template(values, tmp_path, show_only="templates/pvc.yaml")
        assert pvc.returncode != 0 or "PersistentVolumeClaim" not in pvc.stdout

    @requires_helm
    @pytest.mark.parametrize(
        "show_only",
        [
            "templates/deployment.yaml",
            "templates/configmap.yaml",
            "templates/secret.yaml",
        ],
    )
    def test_render_survives_null_bitbucket_block(self, tmp_path, show_only):
        """A null bitbucket block renders as disabled instead of erroring.

        ``helm upgrade --reuse-values`` from a release created before the
        bitbucket block existed leaves ``.Values.bitbucket`` null; the
        templates must treat that as disabled. A nil-pointer error would
        otherwise abort the render.
        """
        values = {"authMode": "api-token", "bitbucket": None}
        result = _helm_template(values, tmp_path, show_only=show_only)
        assert result.returncode == 0, result.stderr
        assert "BITBUCKET" not in result.stdout
