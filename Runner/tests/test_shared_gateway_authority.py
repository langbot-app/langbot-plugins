"""Minimal A/B proof for the optional asset gateway on one Runner object."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from langbot_plugin.api.proxies.invocation import bind_invocation
from langbot_plugin.entities.io.context import InstallationBinding

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["coze-agent", "langflow-agent", "n8n-agent"])
def test_two_bindings_revoke_only_their_own_gateway_authority(folder):
    async def check():
        root = ROOT / folder
        sys.path.insert(0, str(root))
        try:
            spec = importlib.util.spec_from_file_location(f"gateway_{folder}", root / "pkg/asset_gateway.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            handler = object()
            class Owner:
                pass
            owner = Owner()  # one and the same component for both Workspaces
            owner._plugin_runtime_handler = handler
            class API:
                def __init__(self, tenant):
                    self.tenant = tenant
            class Context:
                pass
            config = {"asset_gateway_host": "127.0.0.1", "asset_gateway_port": 0,
                      "asset_gateway_request_timeout": 2, "asset_gateway_token_ttl": 30}
            binding_a = InstallationBinding(instance_uuid="i", workspace_uuid="a", installation_uuid="ia", runtime_revision=1, artifact_digest="a" * 64)
            binding_b = InstallationBinding(instance_uuid="i", workspace_uuid="b", installation_uuid="ib", runtime_revision=1, artifact_digest="a" * 64)
            with bind_invocation(handler, config={"tenant": "a"}, binding=binding_a):
                a = await module.register_assets(owner, API("a"), Context(), config)
                with bind_invocation(handler, config={"tenant": "b"}, binding=binding_b):
                    b = await module.register_assets(owner, API("b"), Context(), config)
                    assert a.gateway is b.gateway  # one listener, one component
                    assert a.token != b.token
                    assert a.gateway._registration_for_token(a.token).tools.api.tenant == "a"
                    assert b.gateway._registration_for_token(b.token).tools.api.tenant == "b"
                    async def probe(tenant, token):
                        reader, writer = await asyncio.open_connection("127.0.0.1", a.gateway.server.sockets[0].getsockname()[1])
                        body = ('{"jsonrpc":"2.0","method":"tools/call","params":{"name":"langbot_get_current_event","arguments":{"run_token":"' + token + '"}},"id":1}').encode()
                        writer.write(b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                        await writer.drain()
                        response = await reader.read()
                        writer.close()
                        await writer.wait_closed()
                        return response
                    async def call_tool(self, name, args):
                        from langbot_plugin.api.proxies.invocation import current_config, current_binding
                        return {"structuredContent": {"tenant": self.api.tenant, "config": current_config(handler)["tenant"], "workspace": current_binding(handler).workspace_uuid}}
                    original = module.AgentRunExternalTools.call_mcp_tool
                    module.AgentRunExternalTools.call_mcp_tool = call_tool
                    try:
                        response_a, response_b = await asyncio.gather(probe("a", a.token), probe("b", b.token))
                        assert b'"tenant": "a", "config": "a", "workspace": "a"' in response_a
                        assert b'"tenant": "b", "config": "b", "workspace": "b"' in response_b
                    finally:
                        module.AgentRunExternalTools.call_mcp_tool = original
                assert b.gateway._registration_for_token(b.token) is None
                assert a.gateway._registration_for_token(a.token) is not None
                await b.stop()
                assert b.gateway._registration_for_token(b.token) is None
                assert a.gateway._registration_for_token(a.token) is not None
                await a.stop()
        finally:
            sys.path.remove(str(root))
    asyncio.run(check())
