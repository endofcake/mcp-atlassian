"""Helm chart tests for injecting additional pod containers."""

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


def _helm_template(*set_json: str) -> subprocess.CompletedProcess[str]:
    """Render the Deployment with optional JSON values overrides."""
    assert _HELM is not None  # guarded by requires_helm
    command = [
        _HELM,
        "template",
        "rel",
        str(_CHART_DIR),
        "--show-only",
        "templates/deployment.yaml",
    ]
    for value in set_json:
        command.extend(("--set-json", value))
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )


def _containers(rendered_deployment: str) -> list[dict]:
    """Return containers from the rendered Deployment pod spec."""
    document = yaml.safe_load(rendered_deployment)
    return document["spec"]["template"]["spec"]["containers"]


def test_chart_declares_extra_containers_defaulting_empty():
    """The values and Deployment templates expose the sidecar contract."""
    values = yaml.safe_load(_VALUES.read_text())
    assert values["extraContainers"] == []
    assert ".Values.extraContainers" in _DEPLOYMENT_TEMPLATE.read_text()


@requires_helm
def test_default_render_has_only_main_container():
    """The default empty value does not add a container."""
    result = _helm_template()
    assert result.returncode == 0, result.stderr

    containers = _containers(result.stdout)
    assert [container["name"] for container in containers] == ["mcp-atlassian"]


@requires_helm
def test_extra_containers_are_appended_verbatim():
    """Additional container specs follow the main application container."""
    sidecars = (
        'extraContainers=[{"name":"log-shipper","image":"fluent/fluent-bit:3.1",'
        '"args":["--workdir=/fluent-bit/etc"],"env":[{"name":"LOG_LEVEL",'
        '"value":"info"}],"volumeMounts":[{"name":"varlog","mountPath":"/var/log"}]}]'
    )
    volumes = 'volumes=[{"name":"varlog","emptyDir":{}}]'

    result = _helm_template(sidecars, volumes)
    assert result.returncode == 0, result.stderr

    containers = _containers(result.stdout)
    assert [container["name"] for container in containers] == [
        "mcp-atlassian",
        "log-shipper",
    ]
    assert containers[1] == {
        "name": "log-shipper",
        "image": "fluent/fluent-bit:3.1",
        "args": ["--workdir=/fluent-bit/etc"],
        "env": [{"name": "LOG_LEVEL", "value": "info"}],
        "volumeMounts": [{"name": "varlog", "mountPath": "/var/log"}],
    }
