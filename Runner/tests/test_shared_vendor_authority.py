"""Assert escaped gateway subprocesses do not inherit tenant authority."""
import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest
from langbot_plugin.api.proxies.invocation import bind_invocation

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["tbox-agent", "dashscope-agent"])
def test_vendor_subprocess_runs_without_inherited_binding(folder, tmp_path):
    async def check():
        root = ROOT / folder
        path = root / "pkg/vendor_process.py"
        spec = importlib.util.spec_from_file_location(f"vendor_{folder}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        script = tmp_path / "worker.py"
        script.write_text("import json,sys\nfrom langbot_plugin.api.proxies.invocation import current_invocation\njson.loads(sys.stdin.readline())\nprint(json.dumps({'item': current_invocation() is None}), flush=True)\n")
        with bind_invocation(object(), config={"api-key": "tenant-secret"}):
            result = [item async for item in module.vendor_stream({}, timeout=3, worker=script)]
            assert result == [True]
    asyncio.run(check())
