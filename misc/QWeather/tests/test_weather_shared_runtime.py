"""QWeather: per-installation config and a non-blocking async HTTP path."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from langbot_plugin.api.proxies.invocation import bind_invocation, current_config
from langbot_plugin.entities.io.context import InstallationBinding

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pkg.weather_data as weather_data  # noqa: E402


def _binding(installation: str, *, workspace: str = "workspace-qweather"):
    return InstallationBinding(
        instance_uuid="instance-1",
        workspace_uuid=workspace,
        installation_uuid=installation,
        runtime_revision=1,
        artifact_digest="b" * 64,
    )


class _StubPlugin:
    """Per-invocation config, exactly as the SDK's BasePlugin.get_config."""

    def __init__(self, handler):
        self.plugin_runtime_handler = handler

    def get_config(self) -> dict:
        return dict(current_config(self.plugin_runtime_handler) or {})

    def get_plugin_config(self) -> dict:
        return self.get_config()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _payload_for(url: str) -> dict:
    if "city/" in url:
        return {"code": "200", "location": [{"name": "Testville", "id": "101"}]}
    if "warning/now" in url:
        return {"code": "204"}
    if "air/now" in url:
        return {
            "code": "200",
            "now": {"category": "Good", "aqi": "50", "pm2p5": "12", "pm10": "20",
                    "o3": "30", "co": "0.5", "no2": "10", "so2": "5"},
        }
    if "weather/24h" in url:
        return {"code": "200", "hourly": [{"fxTime": "2026-01-01T00:00", "temp": "5", "icon": "100", "text": "Clear"}]}
    if "indices" in url:
        return {"code": "200", "daily": [{"name": "Sport", "category": "sport", "text": "Good"}]}
    if "astronomy/sun" in url:
        return {"code": "200", "sunrise": "06:00", "sunset": "18:00"}
    if url.endswith("/weather/now"):
        return {
            "code": "200",
            "now": {"obsTime": "2026-01-01T00:00+08:00", "temp": "15", "icon": "100",
                    "text": "Sunny", "windScale": "3", "windDir": "N", "humidity": "45",
                    "precip": "0", "vis": "10"},
        }
    return {"code": "200", "daily": [{"fxDate": "2026-01-01", "tempMax": "9", "tempMin": "1",
                                      "textDay": "Sunny", "textNight": "Clear",
                                      "iconDay": "100", "iconNight": "150"}]}


class _SlowHttp:
    """Async stand-in for ``httpx.AsyncClient``'s request (the network boundary)."""

    def __init__(self, delay: float):
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        await asyncio.sleep(self.delay)
        return _FakeResponse(_payload_for(url))


class WeatherSharedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._original_get_data = weather_data._get_data

    def tearDown(self) -> None:
        weather_data._get_data = self._original_get_data

    async def _run_command(self, binding, config, slow: _SlowHttp, command, handler):
        weather_data._get_data = slow
        context = SimpleNamespace(crt_params=["Testville"])

        async def collect():
            return [
                item
                async for item in command.registered_subcommands["*"].subcommand(command, context)
            ]

        with bind_invocation(handler, config=config, binding=binding):
            task = asyncio.ensure_future(collect())
            ticks = 0
            while not task.done():
                await asyncio.sleep(0.02)
                ticks += 1
            results = await task
        return results, ticks

    async def test_handler_yields_the_loop_and_uses_the_invocation_config(self) -> None:
        from components.commands.weather import Weather as WeatherCommand

        binding_a = _binding("installation-qweather-a")
        binding_b = _binding("installation-qweather-b")
        slow = _SlowHttp(0.2)

        # ONE command object and one plugin serve both installations.
        handler = SimpleNamespace()
        command = WeatherCommand()
        command.plugin = _StubPlugin(handler)

        results_a, ticks_a = await self._run_command(
            binding_a, {"qweather_apikey": "KEY-A", "qweather_apitype": "0"}, slow, command, handler
        )
        results_b, ticks_b = await self._run_command(
            binding_b, {"qweather_apikey": "KEY-B", "qweather_apitype": "0"}, slow, command, handler
        )

        # The handler awaited the slow network boundary instead of holding the loop:
        # a concurrent 20 ms ticker kept running. Pre-fix the synchronous
        # requests.Session().get fan-out starved it and this stayed near zero.
        self.assertGreaterEqual(ticks_a, 5)
        self.assertGreaterEqual(ticks_b, 5)

        self.assertIn("温度", results_a[0].text)
        self.assertIsNone(results_a[0].error)

        keys_a = [params.get("key") for _, params in slow.calls[:8]]
        keys_b = [params.get("key") for _, params in slow.calls[8:]]
        self.assertEqual(keys_a, ["KEY-A"] * 8)
        self.assertEqual(keys_b, ["KEY-B"] * 8)
        self.assertNotIn("KEY-B", keys_a)
        self.assertNotIn("KEY-A", keys_b)


if __name__ == "__main__":
    unittest.main()
