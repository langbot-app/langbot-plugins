"""Real SDK controller attaches two Workspaces to each gateway Runner object."""
import asyncio
import os
import sys
from pathlib import Path
import pytest
import yaml
from langbot_plugin.api.definition.components.manifest import ComponentManifest
from langbot_plugin.cli.run.controller import PluginRuntimeController
from langbot_plugin.entities.io.context import InstallationBinding

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("folder", ["coze-agent", "langflow-agent", "n8n-agent", "dashscope-agent", "tbox-agent"])
def test_two_workspace_slots_share_real_plugin_and_runner(folder, monkeypatch):
    async def check():
        root = ROOT / folder
        monkeypatch.chdir(root)
        sys.path.insert(0, str(root))
        for name in list(sys.modules):
            if name == "pkg" or name.startswith("pkg.") or name in ("main", "components.runner.default"):
                sys.modules.pop(name, None)
        try:
            plugin = ComponentManifest(owner="langbot-team", manifest=yaml.safe_load((root / "manifest.yaml").read_text()), rel_path="manifest.yaml")
            runner = ComponentManifest(owner="langbot-team", manifest=yaml.safe_load((root / "components/runner/default.yaml").read_text()), rel_path="components/runner/default.yaml")
            controller = PluginRuntimeController(plugin_manifest=plugin, component_manifests=[runner], stdio=True, ws_debug_url="ws://localhost/unused")
            controller.handler = object()
            a = InstallationBinding(instance_uuid="i", workspace_uuid="a", installation_uuid="ia", runtime_revision=1, artifact_digest="a" * 64)
            b = a.model_copy(update={"workspace_uuid": "b", "installation_uuid": "ib"})
            slot_a = await controller.initialize_slot(a, {"enabled": True, "priority": 0, "plugin_config": {"tenant": "a"}})
            slot_b = await controller.initialize_slot(b, {"enabled": True, "priority": 0, "plugin_config": {"tenant": "b"}})
            assert slot_a.plugin_container is slot_b.plugin_container
            assert slot_a.plugin_container.plugin_instance is slot_b.plugin_container.plugin_instance
            assert slot_a.plugin_container.components[0].component_instance is slot_b.plugin_container.components[0].component_instance
            assert slot_a.plugin_config["tenant"] == "a" and slot_b.plugin_config["tenant"] == "b"
            await controller.detach_slot("ia")
            assert controller.plugin_container_for_slot("ib") is slot_b
        finally:
            sys.path.remove(str(root))
            for name in list(sys.modules):
                if name == "pkg" or name.startswith("pkg.") or name in ("main", "components.runner.default"):
                    sys.modules.pop(name, None)
    asyncio.run(check())
