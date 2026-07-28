"""Helm chart tests for the server TLS (HTTPS listener) wiring.

These guard that enabling ``tls.enabled`` mounts the operator-provided Secret
read-only, passes ``--ssl-certfile``/``--ssl-keyfile`` to the server, disables
the OS trust store injection (the HTTPS listener requires it), and defaults the
readiness probe to HTTPS — and that misconfigurations fail the render loudly
instead of yielding a pod that serves plaintext or crashloops. The chart never
creates a certificate. The render tests shell out to ``helm`` when it is on
PATH and skip otherwise; the text/values contract tests always run.
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
_VALUES = _CHART_DIR / "values.yaml"

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


def _pod_spec(rendered_deployment: str) -> dict:
    """Return the pod spec from a rendered Deployment manifest."""
    doc = yaml.safe_load(rendered_deployment)
    return doc["spec"]["template"]["spec"]


def _env_dict(container: dict) -> dict:
    """Map the container's literal env entries to name -> value."""
    return {e["name"]: e.get("value") for e in container.get("env", []) if "value" in e}


def _tls_values(**tls_overrides: object) -> dict:
    """Build chart values with TLS enabled on an HTTP transport."""
    tls = {"enabled": True, "secretName": "mcp-tls"}
    tls.update(tls_overrides)
    return {"transport": "streamable-http", "authMode": "api-token", "tls": tls}


class TestServerTLSHelmContract:
    """The chart wires the server's HTTPS listener from an existing Secret."""

    def test_deployment_references_ssl_flags(self):
        """The deployment passes the listener cert/key flags to the server."""
        template = _DEPLOYMENT_TEMPLATE.read_text()
        for flag in ("--ssl-certfile", "--ssl-keyfile"):
            assert flag in template, f"deployment.yaml is missing {flag}"

    def test_values_declares_tls_block_defaulting_off(self):
        """values.yaml ships a tls block, disabled, with k8s/tls key defaults."""
        values = yaml.safe_load(_VALUES.read_text())
        assert values["tls"]["enabled"] is False
        assert values["tls"]["certFileKey"] == "tls.crt"
        assert values["tls"]["keyFileKey"] == "tls.key"
        assert values["caBundle"]["secretName"] == ""

    @requires_helm
    def test_render_wires_args_volume_mount_probe_and_env(self, tmp_path):
        """Enabling TLS wires the flags, a read-only Secret mount, an HTTPS
        readiness probe, and disables the OS trust store injection (which the
        HTTPS listener requires)."""
        result = _helm_template(_tls_values(), tmp_path)
        assert result.returncode == 0, result.stderr
        spec = _pod_spec(result.stdout)
        container = spec["containers"][0]

        args = container["args"]
        assert args[args.index("--ssl-certfile") + 1] == (
            "/etc/mcp-atlassian/tls/tls.crt"
        )
        assert args[args.index("--ssl-keyfile") + 1] == (
            "/etc/mcp-atlassian/tls/tls.key"
        )

        assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
        assert _env_dict(container)["MCP_ATLASSIAN_USE_SYSTEM_TRUSTSTORE"] == "false"

        volume = next(v for v in spec["volumes"] if v["name"] == "tls-certs")
        assert volume["secret"]["secretName"] == "mcp-tls"
        assert volume["secret"]["defaultMode"] == 256  # octal 0400: owner read-only
        # Only the named entries are projected; unrelated keys in a reused
        # Secret must not reach the pod filesystem.
        assert {i["key"] for i in volume["secret"]["items"]} == {"tls.crt", "tls.key"}
        mount = next(m for m in container["volumeMounts"] if m["name"] == "tls-certs")
        assert mount["mountPath"] == "/etc/mcp-atlassian/tls"
        assert mount["readOnly"] is True

    @requires_helm
    def test_render_custom_keys_and_mountpath(self, tmp_path):
        """certFileKey/keyFileKey/mountPath compose into the flag paths."""
        result = _helm_template(
            _tls_values(
                certFileKey="server.pem", keyFileKey="server.key", mountPath="/tls"
            ),
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        args = _pod_spec(result.stdout)["containers"][0]["args"]
        assert args[args.index("--ssl-certfile") + 1] == "/tls/server.pem"
        assert args[args.index("--ssl-keyfile") + 1] == "/tls/server.key"

    @requires_helm
    def test_render_disabled_tls_omits_everything(self, tmp_path):
        """With TLS off: no flags, no mount, no HTTPS probe scheme, and no
        forced truststore env."""
        result = _helm_template(
            {
                "transport": "streamable-http",
                "authMode": "api-token",
                "tls": {"enabled": False},
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        container = _pod_spec(result.stdout)["containers"][0]
        assert "--ssl-certfile" not in container["args"]
        assert all(
            m["name"] != "tls-certs" for m in (container.get("volumeMounts") or [])
        )
        assert container["readinessProbe"]["httpGet"].get("scheme") is None
        assert "MCP_ATLASSIAN_USE_SYSTEM_TRUSTSTORE" not in _env_dict(container)

    @requires_helm
    def test_render_tcpsocket_probe_skips_scheme(self, tmp_path):
        """A non-httpGet probe — e.g. an operator that nulls httpGet and sets
        tcpSocket — gets no scheme injected; scheme injection is httpGet-only."""
        values = _tls_values()
        values["readinessProbe"] = {
            "httpGet": None,
            "tcpSocket": {"port": "http"},
        }
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr
        probe = _pod_spec(result.stdout)["containers"][0]["readinessProbe"]
        assert probe.get("httpGet") is None
        assert "scheme" not in (probe.get("tcpSocket") or {})

    @requires_helm
    def test_render_explicit_probe_scheme_is_respected(self, tmp_path):
        """An operator-set probe scheme wins over the HTTPS default."""
        values = _tls_values()
        values["readinessProbe"] = {
            "httpGet": {"path": "/healthz", "port": "http", "scheme": "HTTP"}
        }
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr
        container = _pod_spec(result.stdout)["containers"][0]
        assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTP"

    @requires_helm
    def test_render_enabled_without_secret_fails_loudly(self, tmp_path):
        """Enabling TLS without a Secret name aborts the render."""
        values = _tls_values()
        del values["tls"]["secretName"]
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "tls.secretName is required" in result.stderr

    @requires_helm
    def test_render_tls_with_stdio_fails_loudly(self, tmp_path):
        """TLS under stdio aborts rather than silently dropping the cert."""
        values = _tls_values()
        values["transport"] = "stdio"
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "requires an HTTP transport" in result.stderr

    @requires_helm
    @pytest.mark.parametrize(
        "mount_path", ["/app/tls", "/app", "//app/tls", "/./app/tls"]
    )
    def test_render_tls_mountpath_under_app_fails_loudly(self, tmp_path, mount_path):
        """/app is the confinement root for caller-supplied file paths, so a
        key mounted under it would be tenant-readable via the attachment
        tools; the render must refuse — including noncanonical spellings."""
        result = _helm_template(_tls_values(mountPath=mount_path), tmp_path)
        assert result.returncode != 0
        assert "must not be /app or a path under /app" in result.stderr

    @requires_helm
    def test_render_mountpath_outside_app_is_allowed(self, tmp_path):
        """A path that merely shares the /app string prefix (e.g.
        /application) is outside the boundary and must render."""
        result = _helm_template(_tls_values(mountPath="/application/tls"), tmp_path)
        assert result.returncode == 0, result.stderr
        args = _pod_spec(result.stdout)["containers"][0]["args"]
        assert args[args.index("--ssl-certfile") + 1] == "/application/tls/tls.crt"

    @requires_helm
    def test_render_relative_mountpath_fails_loudly(self, tmp_path):
        """Relative mount paths are refused (the boundary check requires a
        canonical absolute path)."""
        result = _helm_template(_tls_values(mountPath="tls"), tmp_path)
        assert result.returncode != 0
        assert "must be an absolute path" in result.stderr

    @requires_helm
    @pytest.mark.parametrize("bad_key", [".", "..", "sub/dir.crt"])
    def test_render_non_basename_secret_key_fails_loudly(self, tmp_path, bad_key):
        """Secret key selectors must be plain file names; '.' or a path would
        compose a directory or traverse the mount."""
        result = _helm_template(_tls_values(certFileKey=bad_key), tmp_path)
        assert result.returncode != 0
        assert "plain file names" in result.stderr

    @requires_helm
    def test_render_ca_bundle_mountpath_under_app_fails_loudly(self, tmp_path):
        """Same confinement rule for the CA bundle mount."""
        values = _tls_values()
        values["caBundle"] = {"secretName": "corp-ca", "mountPath": "//app/ca"}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "caBundle.mountPath must not be /app or a path under /app" in (
            result.stderr
        )

    @requires_helm
    def test_render_legacy_value_shape_survives(self, tmp_path):
        """helm upgrade --reuse-values from chart 1.0 supplies no tls/caBundle
        maps at all (null after merge); the render must still succeed."""
        result = _helm_template(
            {
                "transport": "streamable-http",
                "authMode": "api-token",
                "tls": None,
                "caBundle": None,
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        container = _pod_spec(result.stdout)["containers"][0]
        assert "--ssl-certfile" not in container["args"]

    @requires_helm
    def test_render_null_readiness_probe_survives(self, tmp_path):
        """An operator nulling the probe block must not break the render
        when TLS is enabled (probe-scheme injection is httpGet-only)."""
        values = _tls_values()
        values["readinessProbe"] = None
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr


class TestCaBundleHelmContract:
    """Optional outbound private-CA bundle mount and env wiring."""

    @requires_helm
    def test_render_ca_bundle_sets_both_env_vars_and_mount(self, tmp_path):
        """A caBundle Secret is mounted read-only and exposed through BOTH
        REQUESTS_CA_BUNDLE (requests clients) and SSL_CERT_FILE (httpx
        paths)."""
        values = _tls_values()
        values["caBundle"] = {"secretName": "corp-ca"}
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr
        spec = _pod_spec(result.stdout)
        container = spec["containers"][0]

        env = _env_dict(container)
        assert env["REQUESTS_CA_BUNDLE"] == "/etc/mcp-atlassian/ca/ca.crt"
        assert env["SSL_CERT_FILE"] == "/etc/mcp-atlassian/ca/ca.crt"

        volume = next(v for v in spec["volumes"] if v["name"] == "ca-bundle")
        assert volume["secret"]["secretName"] == "corp-ca"
        mount = next(m for m in container["volumeMounts"] if m["name"] == "ca-bundle")
        assert mount["readOnly"] is True

    @requires_helm
    def test_render_ca_bundle_without_tls_is_allowed(self, tmp_path):
        """The CA bundle is independent of the listener: usable on a plain
        HTTP deployment (e.g. behind an ingress) with a private-CA Atlassian."""
        result = _helm_template(
            {
                "transport": "streamable-http",
                "authMode": "api-token",
                "caBundle": {"secretName": "corp-ca"},
            },
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        env = _env_dict(_pod_spec(result.stdout)["containers"][0])
        assert env["REQUESTS_CA_BUNDLE"] == "/etc/mcp-atlassian/ca/ca.crt"
        assert "MCP_ATLASSIAN_USE_SYSTEM_TRUSTSTORE" not in env

    @requires_helm
    def test_render_ca_bundle_blank_key_uses_default(self, tmp_path):
        """A blank caBundle.key means unset and falls back to the default
        (consistent with the reuse-values defaulting of every tls/caBundle
        field); a non-basename key is what aborts."""
        values = _tls_values()
        values["caBundle"] = {"secretName": "corp-ca", "key": ""}
        result = _helm_template(values, tmp_path)
        assert result.returncode == 0, result.stderr
        env = _env_dict(_pod_spec(result.stdout)["containers"][0])
        assert env["REQUESTS_CA_BUNDLE"] == "/etc/mcp-atlassian/ca/ca.crt"

    @requires_helm
    def test_render_ca_bundle_non_basename_key_fails_loudly(self, tmp_path):
        """A path-like caBundle.key would traverse the mount; abort."""
        values = _tls_values()
        values["caBundle"] = {"secretName": "corp-ca", "key": "sub/ca.crt"}
        result = _helm_template(values, tmp_path)
        assert result.returncode != 0
        assert "caBundle.key must be a plain file name" in result.stderr


class TestIngressLabelsHelmContract:
    """Custom labels merge into the Ingress metadata."""

    @staticmethod
    def _ingress_values(**ingress_overrides: object) -> dict:
        ingress = {
            "enabled": True,
            "hosts": [
                {
                    "host": "mcp.example.com",
                    "paths": [{"path": "/", "pathType": "Prefix"}],
                }
            ],
        }
        ingress.update(ingress_overrides)
        return {
            "transport": "streamable-http",
            "authMode": "api-token",
            "ingress": ingress,
        }

    @requires_helm
    def test_render_ingress_custom_labels_merge(self, tmp_path):
        """ingress.labels entries appear alongside the chart's standard
        labels."""
        result = _helm_template(
            self._ingress_values(labels={"team": "platform", "tier": "edge"}),
            tmp_path,
            show_only="templates/ingress.yaml",
        )
        assert result.returncode == 0, result.stderr
        labels = yaml.safe_load(result.stdout)["metadata"]["labels"]
        assert labels["team"] == "platform"
        assert labels["tier"] == "edge"
        assert "app.kubernetes.io/name" in labels  # standard labels retained

    @requires_helm
    def test_render_ingress_label_collision_chart_wins_single_key(self, tmp_path):
        """A custom label colliding with a standard one must not emit a
        duplicate YAML key (strict consumers reject it), and the chart's value
        wins. Checked textually — yaml.safe_load would mask a duplicate."""
        result = _helm_template(
            self._ingress_values(labels={"app.kubernetes.io/name": "custom"}),
            tmp_path,
            show_only="templates/ingress.yaml",
        )
        assert result.returncode == 0, result.stderr
        name_lines = [
            line
            for line in result.stdout.splitlines()
            if line.strip().startswith("app.kubernetes.io/name:")
        ]
        assert len(name_lines) == 1
        assert "custom" not in name_lines[0]

    @requires_helm
    def test_render_ingress_without_custom_labels_unchanged(self, tmp_path):
        """Without ingress.labels the metadata carries only standard labels."""
        result = _helm_template(
            self._ingress_values(),
            tmp_path,
            show_only="templates/ingress.yaml",
        )
        assert result.returncode == 0, result.stderr
        labels = yaml.safe_load(result.stdout)["metadata"]["labels"]
        assert "team" not in labels
        assert "app.kubernetes.io/name" in labels
