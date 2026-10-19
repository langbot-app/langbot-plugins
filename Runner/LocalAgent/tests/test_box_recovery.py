"""Export errors are ordinary tool results; no completion recovery hook."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.entities.builtin.runner.box import BoxFile

from pkg.agent_core.langbot import LangBotToolExecutor
from pkg.agent_core.types import ToolCallRequest
from pkg.box import LocalAgentBox


@pytest.mark.asyncio
async def test_explicit_export_error_then_retry():
    ctx = SimpleNamespace(
        export_box_files=AsyncMock(
            side_effect=[RuntimeError("file missing: /out/a"), [BoxFile(id="file", name="a", type="File", size=1)]]
        ),
        delivery=SimpleNamespace(automatic_reply=True),
        reply_files=AsyncMock(),
    )
    box = LocalAgentBox(ctx, None)
    executor = LangBotToolExecutor(None, set())
    assembly = SimpleNamespace(tools=[], tool_executor=executor)
    box.install_tool(assembly)
    request = ToolCallRequest.from_raw(
        {
            "id": "export",
            "type": "function",
            "function": {"name": "export_box_files", "arguments": '{"paths": ["/out/a"]}'},
        }
    )
    failed = await executor.execute(executor.prepare(request))
    assert "file missing" in failed.error
    assert not box.file_ids
    success = await executor.execute(executor.prepare(request))
    assert success.error is None
    assert box.file_ids == ["file"]
    assert not hasattr(box, "before_complete")
    assert not hasattr(box, "finish")
