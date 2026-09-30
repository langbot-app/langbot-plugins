"""Two installations, one ScheNotify object: schedules never cross over.

Evidence model: the real SDK ``bind_invocation``/``InstallationBinding`` drive
two distinct installation bindings through one plugin object; the stub Host
records every ``SEND_MESSAGE`` relay it receives, so a notification fired for
the wrong installation is visible as an extra relay carrying the other
installation's reminder text.

Pre-fix failure (asserted below): the plugin kept one ``scheduled_events`` list
and one minute-loop on the process-wide object, created in ``initialize()``.
Installation B's ``get_scheduled_events`` therefore returned installation A's
reminders, and the single loop delivered both installations' reminders (the
loop also resolved config outside any invocation). ``on_installation_revoked``
released nothing because nothing was keyed by an installation.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from langbot_plugin.api.proxies.invocation import bind_invocation
from langbot_plugin.entities.io.actions.enums import PluginToRuntimeAction as Action
from langbot_plugin.entities.io.context import InstallationBinding

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_main():
    spec = importlib.util.spec_from_file_location("schenotify_main", PLUGIN_ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sche_main = _load_main()


class FlatHost:
    """Stub Host that records notification relays (no live platform call)."""

    def __init__(self):
        self.sent: list[dict] = []

    async def call_action(self, action, data, *args, **kwargs):
        if action == Action.SEND_MESSAGE:
            self.sent.append(data)
            return {"result": "ok"}
        raise AssertionError(f"unexpected Host action: {action}")


def binding(installation: str, *, workspace: str = "workspace-1"):
    return InstallationBinding(
        instance_uuid="instance-1",
        workspace_uuid=workspace,
        installation_uuid=installation,
        placement_generation=1,
        runtime_revision=1,
        artifact_digest="a" * 64,
    )


def build_plugin():
    plugin = sche_main.ScheNotify()
    host = FlatHost()
    plugin.plugin_runtime_handler = host
    return plugin, host


def _texts(payloads: list[dict]) -> list[str]:
    texts = []
    for payload in payloads:
        texts.append(
            "".join(
                part.get("text", "")
                for part in payload["message_chain"]
                if part.get("type") == "Plain"
            )
        )
    return texts


PAST = datetime.datetime(2020, 1, 1, 0, 0, 0)
FUTURE = datetime.datetime(2999, 1, 1, 0, 0, 0)


def test_schedule_is_per_installation():
    async def scenario():
        plugin, host = build_plugin()
        first, second = binding("installation-a"), binding("installation-b")

        with bind_invocation(host, config={"language": "en_US"}, binding=first):
            await plugin.add_scheduled_event(
                time=PAST, message="A reminder", bot_uuid="bot", target_type="person", target_id="1"
            )
        with bind_invocation(host, config={"language": "zh_Hans"}, binding=second):
            await plugin.add_scheduled_event(
                time=PAST, message="B reminder", bot_uuid="bot", target_type="person", target_id="2"
            )

        with bind_invocation(host, config={"language": "zh_Hans"}, binding=second):
            # Pre-fix this returned ["A reminder", "B reminder"] from the shared list.
            assert [e["message"] for e in await plugin.get_scheduled_events()] == ["B reminder"]

        # A's scheduler fires A's reminder only; B's stays pending.
        await plugin._check_scheduled_events(plugin._scope(first))
        assert _texts(host.sent) == ["[Notify] A reminder"]
        with bind_invocation(host, config={"language": "zh_Hans"}, binding=second):
            assert [e["message"] for e in await plugin.get_scheduled_events()] == ["B reminder"]

        await plugin._check_scheduled_events(plugin._scope(second))
        assert _texts(host.sent) == ["[Notify] A reminder", "[Notify] B reminder"]

        # Nothing is left pending in either installation.
        assert plugin._events[plugin._scope(first)] == []
        assert plugin._events[plugin._scope(second)] == []

    asyncio.run(scenario())


def test_scheduler_is_registered_per_binding_and_stopped_on_revocation():
    async def scenario():
        plugin, host = build_plugin()
        first, second = binding("installation-a"), binding("installation-b")
        started: list[str] = []

        async def fake_loop(scope):
            started.append(scope)
            await asyncio.sleep(3600)

        plugin._check_loop_wrapper = fake_loop

        with bind_invocation(host, config={}, binding=first):
            await plugin.add_scheduled_event(
                time=FUTURE, message="A reminder", bot_uuid="bot", target_type="person", target_id="1"
            )
        with bind_invocation(host, config={}, binding=second):
            await plugin.add_scheduled_event(
                time=FUTURE, message="B reminder", bot_uuid="bot", target_type="person", target_id="2"
            )
        await asyncio.sleep(0)

        assert started == [plugin._scope(first), plugin._scope(second)]
        first_task = plugin._loops[plugin._scope(first)]

        await plugin.on_installation_revoked(first)
        assert first_task.cancelled() or first_task.done()
        assert plugin._scope(first) not in plugin._loops
        assert plugin._scope(first) not in plugin._events
        # A revoked installation's schedule is dropped: no late delivery.
        before = len(host.sent)
        await plugin._check_scheduled_events(plugin._scope(first))
        assert len(host.sent) == before
        # The sibling installation is untouched.
        assert plugin._scope(second) in plugin._loops
        assert [e["message"] for e in plugin._events[plugin._scope(second)]] == ["B reminder"]

        await plugin.on_installation_revoked(second)

    asyncio.run(scenario())


def test_detached_scheduler_reads_no_invocation_config():
    async def scenario():
        plugin, host = build_plugin()
        first = binding("installation-a")

        with bind_invocation(host, config={"language": "en_US"}, binding=first):
            await plugin.add_scheduled_event(
                time=PAST, message="A reminder", bot_uuid="bot", target_type="person", target_id="1"
            )

        def refuse():
            raise AssertionError("detached scheduler read invocation config")

        plugin.get_config = refuse
        # Pre-fix _send_notification read self.get_config() on every delivery.
        await plugin._check_scheduled_events(plugin._scope(first))
        assert _texts(host.sent) == ["[Notify] A reminder"]

    asyncio.run(scenario())


def test_scope_key_is_the_full_binding():
    plugin, _host = build_plugin()
    assert plugin._scope(binding("installation-a")) == "instance-1:workspace-1:installation-a"
    assert plugin._scope(binding("installation-a")) != plugin._scope(binding("installation-a", workspace="workspace-2"))
    assert plugin._scope() == sche_main.LEGACY_SCOPE
    assert isinstance(plugin._scope(SimpleNamespace(instance_uuid="i", workspace_uuid="w", installation_uuid="x")), str)


def test_initialize_starts_no_scheduler():
    async def scenario():
        plugin, _host = build_plugin()
        await plugin.initialize()
        assert plugin._loops == {}

    asyncio.run(scenario())


def test_dedicated_binding_less_invocation_still_schedules():
    """A dedicated worker's invocation carries no binding; it keeps its own scope."""

    async def scenario():
        plugin, host = build_plugin()
        loop_scopes: list[str] = []

        async def fake_loop(scope):
            loop_scopes.append(scope)
            await asyncio.sleep(3600)

        plugin._check_loop_wrapper = fake_loop
        with bind_invocation(host, config={"language": "en_US"}):
            await plugin.add_scheduled_event(
                time=PAST, message="dedicated", bot_uuid="bot", target_type="person", target_id="1"
            )
        await asyncio.sleep(0)
        assert loop_scopes == [sche_main.LEGACY_SCOPE]

        await plugin._check_scheduled_events(sche_main.LEGACY_SCOPE)
        assert _texts(host.sent) == ["[Notify] dedicated"]
        await plugin.on_installation_revoked(binding("installation-a"))
        assert plugin._loops  # a revoked binding does not disturb the legacy scope

    asyncio.run(scenario())
