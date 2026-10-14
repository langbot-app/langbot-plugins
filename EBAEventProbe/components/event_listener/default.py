from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
from langbot_plugin.api.entities.builtin.platform import message as platform_message


TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class EBAEventProbeListener(EventListener):
    def __init__(self):
        super().__init__()
        self.log_path = Path(os.getenv("EBA_PROBE_LOG", "eba_event_probe.jsonl"))
        self.api_probe_enabled = os.getenv("EBA_PROBE_API") == "1"
        self.component_sweep_enabled = os.getenv("EBA_PROBE_COMPONENT_SWEEP") == "1"
        self.platform_api_probe_enabled = os.getenv("EBA_PROBE_PLATFORM_API") == "1"
        self.destructive_probe_enabled = os.getenv("EBA_PROBE_DESTRUCTIVE") == "1"
        self.api_probe_done = False

        for event_type in (
            events.MessageReceived,
            events.MessageEdited,
            events.MessageDeleted,
            events.MessageReactionReceived,
            events.FeedbackReceived,
            events.GroupMemberJoined,
            events.GroupMemberLeft,
            events.GroupMemberBanned,
            events.BotInvitedToGroup,
            events.BotRemovedFromGroup,
            events.BotMuted,
            events.BotUnmuted,
            events.PlatformSpecificEventReceived,
        ):
            self.handler(event_type)(self._record)

    async def _record(self, event_context: context.EventContext):
        record = {
            "event_name": event_context.event_name,
            "query_id": event_context.query_id,
            "event": event_context.event.model_dump(),
        }
        with self.log_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"EBA_PROBE_EVENT {event_context.event_name}")

        if (
            self.api_probe_enabled
            and not self.api_probe_done
            and isinstance(event_context.event, events.MessageReceived)
        ):
            self.api_probe_done = True
            await self._probe_plugin_apis(event_context.event)

    async def _probe_plugin_apis(self, event: events.MessageReceived):
        api_result = {"event_name": "APIProbe", "ok": True, "calls": []}

        try:
            version = await self.plugin.get_langbot_version()
            api_result["calls"].append({"name": "get_langbot_version", "result": version})

            bots = await self.plugin.get_bots()
            api_result["calls"].append({"name": "get_bots", "result": bots})

            if bots:
                selected_bot = next(
                    (
                        bot
                        for bot in bots
                        if isinstance(bot, dict) and bot.get("uuid") == event.bot_uuid
                    ),
                    next((bot for bot in bots if isinstance(bot, dict) and bot.get("enable")), bots[0]),
                )
                selected_bot_uuid = selected_bot["uuid"] if isinstance(selected_bot, dict) else selected_bot

                bot_info = await self.plugin.get_bot_info(selected_bot_uuid)
                api_result["calls"].append({"name": "get_bot_info", "result": bot_info})

                target_type = "group" if event.chat_type == "group" else "person"
                send_result = await self.plugin.send_message(
                    selected_bot_uuid,
                    target_type,
                    event.chat_id,
                    platform_message.MessageChain(
                        [platform_message.Plain(text="EBA API probe message")]
                    ),
                )
                api_result["calls"].append({"name": "send_message", "result": send_result or "ok"})

                if self.component_sweep_enabled:
                    api_result["calls"].append(
                        {
                            "name": "component_sweep",
                            "result": await self._probe_outbound_components(
                                selected_bot_uuid, target_type, event
                            ),
                        }
                    )

                if self.platform_api_probe_enabled:
                    api_result["calls"].append(
                        {
                            "name": "platform_api_sweep",
                            "result": await self._probe_platform_apis(selected_bot_uuid, event),
                        }
                    )

            await self.plugin.set_plugin_storage("eba_probe_plugin", b"plugin-value")
            plugin_value = await self.plugin.get_plugin_storage("eba_probe_plugin")
            plugin_keys = await self.plugin.get_plugin_storage_keys()
            await self.plugin.delete_plugin_storage("eba_probe_plugin")
            api_result["calls"].append(
                {
                    "name": "plugin_storage",
                    "result": {"value": plugin_value.decode("utf-8"), "keys": plugin_keys},
                }
            )

            await self.plugin.set_workspace_storage("eba_probe_workspace", b"workspace-value")
            workspace_value = await self.plugin.get_workspace_storage("eba_probe_workspace")
            workspace_keys = await self.plugin.get_workspace_storage_keys()
            await self.plugin.delete_workspace_storage("eba_probe_workspace")
            api_result["calls"].append(
                {
                    "name": "workspace_storage",
                    "result": {
                        "value": workspace_value.decode("utf-8"),
                        "keys": workspace_keys,
                    },
                }
            )

            manifests = await self.plugin.list_plugins_manifest()
            commands = await self.plugin.list_commands()
            tools = await self.plugin.list_tools()
            knowledge_bases = await self.plugin.list_knowledge_bases()
            api_result["calls"].extend(
                [
                    {"name": "list_plugins_manifest", "result": manifests},
                    {"name": "list_commands", "result": commands},
                    {"name": "list_tools", "result": tools},
                    {"name": "list_knowledge_bases", "result": knowledge_bases},
                ]
            )
        except Exception as exc:
            api_result["ok"] = False
            api_result["error"] = repr(exc)

        with self.log_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(api_result, ensure_ascii=False) + "\n")
        print(f"EBA_PROBE_API {'OK' if api_result['ok'] else 'FAILED'}")

    async def _probe_outbound_components(
        self,
        bot_uuid: str,
        target_type: str,
        event: events.MessageReceived,
    ) -> list[dict[str, Any]]:
        cases = [
            (
                "plain_at_face",
                platform_message.MessageChain(
                    [
                        platform_message.Plain(text="EBA component plain+at+face "),
                        platform_message.At(target=event.sender.id),
                        platform_message.Face(face_id=14, face_name="微笑"),
                    ]
                ),
            ),
            (
                "image_base64",
                platform_message.MessageChain(
                    [
                        platform_message.Plain(text="EBA component image "),
                        platform_message.Image(base64=TINY_PNG),
                    ]
                ),
            ),
            (
                "quote",
                platform_message.MessageChain(
                    [
                        platform_message.Quote(
                            id=event.message_id,
                            group_id=event.chat_id if event.chat_type == "group" else None,
                            sender_id=event.sender.id,
                            target_id=event.chat_id,
                            origin=event.message_chain,
                        ),
                        platform_message.Plain(text="EBA component quote"),
                    ]
                ),
            ),
            (
                "file_base64",
                platform_message.MessageChain(
                    [
                        platform_message.Plain(text="EBA component file "),
                        platform_message.File(
                            name="eba-probe.txt",
                            base64="ZmlsZSBmcm9tIEVCQSBwcm9iZQo=",
                            size=20,
                        ),
                    ]
                ),
            ),
            (
                "forward",
                platform_message.MessageChain(
                    [
                        platform_message.Forward(
                            node_list=[
                                platform_message.ForwardMessageNode(
                                    sender_id=event.sender.id,
                                    sender_name=event.sender.nickname or "EBAProbe",
                                    message_chain=platform_message.MessageChain(
                                        [platform_message.Plain(text="forward node from EBA probe")]
                                    ),
                                )
                            ]
                        )
                    ]
                ),
            ),
        ]
        if event.chat_type == "group":
            cases.insert(
                1,
                (
                    "at_all",
                    platform_message.MessageChain(
                        [
                            platform_message.Plain(text="EBA component at_all "),
                            platform_message.AtAll(),
                        ]
                    ),
                ),
            )

        results = []
        for name, message_chain in cases:
            try:
                result = await self.plugin.send_message(
                    bot_uuid,
                    target_type,
                    event.chat_id,
                    message_chain,
                )
                results.append({"name": name, "ok": True, "result": result})
            except Exception as exc:
                results.append({"name": name, "ok": False, "error": repr(exc)})
        return results

    async def _probe_platform_apis(
        self,
        bot_uuid: str,
        event: events.MessageReceived,
    ) -> list[dict[str, Any]]:
        group_id = (
            event.group.id
            if event.chat_type == "group" and event.group
            else event.chat_id if event.chat_type == "group" else None
        )
        user_id = event.sender.id
        calls: list[tuple[str, dict[str, Any]]] = [
            ("get_message", {"chat_type": event.chat_type, "chat_id": event.chat_id, "message_id": event.message_id}),
            ("get_user_info", {"user_id": user_id}),
            ("get_friend_list", {}),
        ]
        if group_id:
            calls.extend(
                [
                    ("get_group_info", {"group_id": group_id}),
                    ("get_group_list", {}),
                    ("get_group_member_list", {"group_id": group_id}),
                    ("get_group_member_info", {"group_id": group_id, "user_id": user_id}),
                ]
            )
        if self.destructive_probe_enabled and group_id:
            calls.extend(
                [
                    ("mute_member", {"group_id": group_id, "user_id": user_id, "duration": 1}),
                    ("unmute_member", {"group_id": group_id, "user_id": user_id}),
                ]
            )

        adapter_name = (event.adapter_name or "").lower()
        if "aiocqhttp" in adapter_name:
            for action, params in (
                ("get_login_info", {}),
                ("get_status", {}),
                ("get_version_info", {}),
                ("can_send_image", {}),
                ("can_send_record", {}),
            ):
                calls.append(("call_platform_api", {"action": action, "params": params}))
            if group_id:
                calls.append(("call_platform_api", {"action": "get_group_honor_info", "params": {"group_id": int(group_id), "type": "all"}}))
        elif "telegram" in adapter_name and group_id:
            calls.extend(
                [
                    ("call_platform_api", {"action": "get_chat_administrators", "params": {"chat_id": group_id}}),
                    ("call_platform_api", {"action": "get_chat_member_count", "params": {"chat_id": group_id}}),
                    (
                        "call_platform_api",
                        {
                            "action": "send_chat_action",
                            "params": {"chat_id": group_id, "action": "typing"},
                        },
                    ),
                ]
            )
        elif "discord" in adapter_name:
            calls.extend(
                [
                    ("call_platform_api", {"action": "get_channel", "params": {"channel_id": event.chat_id}}),
                    ("call_platform_api", {"action": "typing", "params": {"channel_id": event.chat_id}}),
                ]
            )
            guild_id = event.group.id if event.group else None
            if guild_id:
                calls.extend(
                    [
                        ("call_platform_api", {"action": "get_guild", "params": {"guild_id": guild_id}}),
                        ("call_platform_api", {"action": "get_guild_channels", "params": {"guild_id": guild_id}}),
                        ("call_platform_api", {"action": "get_guild_roles", "params": {"guild_id": guild_id}}),
                    ]
                )
            if self.destructive_probe_enabled:
                calls.append(
                    (
                        "call_platform_api",
                        {
                            "action": "add_reaction",
                            "params": {
                                "channel_id": event.chat_id,
                                "message_id": event.message_id,
                                "emoji": "✅",
                            },
                        },
                    )
                )
        elif "dingtalk" in adapter_name:
            calls.append(("call_platform_api", {"action": "check_access_token", "params": {}}))

        results = []
        for name, params in calls:
            try:
                result = await self.plugin.call_platform_api(bot_uuid, name, params)
                results.append({"name": name, "ok": True, "result": result})
            except Exception as exc:
                results.append({"name": name, "ok": False, "error": repr(exc)})
        return results
