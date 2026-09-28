"""Bounded manifest and identity isolation checks against the stateless SDK candidate.

Run with the stateless SDK source on PYTHONPATH; never infer certification from
these checks alone. Network/provider business behavior requires separate tests.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from langbot_plugin.api.definition.components.manifest import Execution
from langbot_plugin.api.proxies.invocation import bind_invocation
from langbot_plugin.entities.io.context import InstallationBinding

ROOT = Path(__file__).resolve().parents[1]
SAFE = {"LocalAgent", "deerflow-agent", "weknora-agent"}
IDENTITY = {"coze-agent", "dify-agent", "n8n-agent", "tbox-agent", "weknora-agent"}
ALL_LEGACY = {
    "LocalAgent", "coze-agent", "dashscope-agent", "deerflow-agent",
    "dify-agent", "langflow-agent", "n8n-agent", "tbox-agent", "weknora-agent",
}


def _binding(installation):
    return InstallationBinding(
        instance_uuid="instance", workspace_uuid="workspace", installation_uuid=installation,
        placement_generation=1, runtime_revision=1, artifact_digest="a" * 64,
    )


@pytest.mark.parametrize("folder", sorted(ALL_LEGACY))
def test_eligibility_is_only_claimed_for_audited_stateless_components(folder):
    data = yaml.safe_load((ROOT / folder / "manifest.yaml").read_text())
    execution = Execution.model_validate(data["execution"])
    assert data["metadata"]["author"] == "langbot-team"
    assert (execution.shared_runtime == "shared-runtime-v1") == (folder in SAFE)
    assert (execution.component_model == "stateless-v1") == (folder in SAFE)
    assert "Runner" in data["spec"]["components"]


@pytest.mark.parametrize("folder", sorted(IDENTITY))
def test_upstream_identity_uses_current_binding_not_process_binding(folder):
    file = ROOT / folder / "pkg" / "scoped_identity.py"
    spec = importlib.util.spec_from_file_location("scoped_identity_candidate", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = SimpleNamespace(bound_action_context=None)
    runner = SimpleNamespace(_plugin_runtime_handler=handler)
    ctx = SimpleNamespace(conversation=SimpleNamespace(workspace_id=None))
    assert module.scoped_identity(runner, ctx, "user") == "user"  # dedicated OSS
    with bind_invocation(handler, binding=_binding("installation-A")):
        a = module.scoped_identity(runner, ctx, "user")
        assert a == module.scoped_identity(runner, ctx, "user")
    with bind_invocation(handler, binding=_binding("installation-B")):
        b = module.scoped_identity(runner, ctx, "user")
    assert a != b and a.startswith("lb_") and b.startswith("lb_")


@pytest.mark.asyncio
async def test_concurrent_upstream_identity_is_isolated_on_one_component():
    file = ROOT / "weknora-agent" / "pkg" / "scoped_identity.py"
    spec = importlib.util.spec_from_file_location("scoped_identity_concurrent", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = SimpleNamespace(bound_action_context=None)
    runner = SimpleNamespace(_plugin_runtime_handler=handler)
    ctx = SimpleNamespace(conversation=SimpleNamespace(workspace_id=None))

    async def identity(installation):
        with bind_invocation(handler, binding=_binding(installation)):
            await asyncio.sleep(0)
            return module.scoped_identity(runner, ctx, "same-user")

    a, b = await asyncio.gather(identity("installation-A"), identity("installation-B"))
    assert a != b
