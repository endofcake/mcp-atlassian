"""Helm chart tests for the Bitbucket Data Center env contract.

These guard against env-contract drift: the chart must wire exactly the
``BITBUCKET_*`` environment variables the server code reads, or a Helm-only
deploy silently degrades. The render tests shell out to ``helm`` when it is on
PATH and skip otherwise; the contract test parses the templates as text and
always runs.
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
# BitbucketConfig.from_env and oauth_upstream._resolve_bitbucket_upstream read;
# omitting any of them degrades the deploy (e.g. a missing redirect URI drops
# the proxy back to bearer-forwarding).
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
        # The new non-secret vars resolve from the ConfigMap, not inline values.
        for name in ("BITBUCKET_OAUTH_REDIRECT_URI", "BITBUCKET_OAUTH_SCOPE"):
            assert "configMapKeyRef" in env[name]["valueFrom"]

    @requires_helm
    def test_render_ssl_verify_opt_out_is_reachable(self, tmp_path):
        """A YAML boolean ``false`` still emits BITBUCKET_SSL_VERIFY=false.

        Regression guard for the truthiness gate: ``sslVerify: false`` (boolean,
        not the string "false") previously emitted no env, so the verification
        opt-out was unreachable.
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
    def test_render_disabled_bitbucket_ignores_authmode(self, tmp_path):
        """The guard only fires when Bitbucket is enabled."""
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
