"""Candidate manifests must opt in only after their authority tests pass."""
from pathlib import Path
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("folder,version", [("coze-agent", "0.1.11"), ("langflow-agent", "0.1.11"), ("n8n-agent", "0.1.11"), ("dashscope-agent", "0.1.11"), ("tbox-agent", "0.1.9")])
def test_gateway_runner_declares_stateless_component(folder, version):
    manifest = yaml.safe_load((ROOT / folder / "manifest.yaml").read_text())
    assert manifest["execution"]["sharedRuntime"] == "shared-runtime-v1"
    assert manifest["execution"]["componentModel"] == "stateless-v1"
    assert manifest["metadata"]["version"] == version
    assert (ROOT / folder / "requirements.txt").read_text().splitlines()[0] == "langbot-plugin>=0.7.3,<0.8"
