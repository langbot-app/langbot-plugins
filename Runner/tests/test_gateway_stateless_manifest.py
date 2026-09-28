"""Candidate manifests must opt in only after their authority tests pass."""
from pathlib import Path
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("folder", ["coze-agent", "langflow-agent", "n8n-agent"])
def test_gateway_runner_declares_stateless_component(folder):
    manifest = yaml.safe_load((ROOT / folder / "manifest.yaml").read_text())
    assert manifest["execution"]["sharedRuntime"] == "shared-runtime-v1"
    assert manifest["execution"]["componentModel"] == "stateless-v1"
    assert manifest["metadata"]["version"] == "0.1.11"
    assert (ROOT / folder / "requirements.txt").read_text().splitlines()[0] == "langbot-plugin>=0.7.3,<0.8"
