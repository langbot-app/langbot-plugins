from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from typing import Any

from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from powercontext_client import PowerContextClient
from scope import LangBotIdentity, binding_key, session_name_from_session


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedPowerContext:
    scope_id: str
    identity: LangBotIdentity
    key: dict[str, str]
    request_id: str | None = None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


class PowerContextPlugin(BasePlugin):
    client: PowerContextClient
    scope_mode: str
    explicit_scope_id: str
    allow_default_scope: bool
    auto_recall: bool
    capture_user_messages: bool
    max_context_bytes: int
    search_limit: int

    async def initialize(self) -> None:
        config = self.get_config()
        self.scope_mode = (
            str(config.get("scope_mode", "session") or "session").strip().lower()
        )
        if self.scope_mode not in {"session", "speaker", "bot"}:
            logger.warning(
                "Invalid scope_mode=%s; falling back to session", self.scope_mode
            )
            self.scope_mode = "session"
        self.explicit_scope_id = str(config.get("scope_id", "") or "").strip()
        self.allow_default_scope = bool(config.get("allow_default_scope", False))
        self.auto_recall = bool(config.get("auto_recall", True))
        self.capture_user_messages = bool(config.get("capture_user_messages", True))
        self.max_context_bytes = _bounded_int(
            config.get("max_context_bytes"), 4000, 512, 32768
        )
        self.search_limit = _bounded_int(config.get("search_limit"), 5, 1, 50)
        timeout_seconds = _bounded_float(config.get("timeout_seconds"), 8.0, 0.5, 60.0)

        self.client = PowerContextClient(
            server_url=str(config.get("server_url", "http://127.0.0.1:8000")),
            api_token=str(
                config.get("api_token", "")
                or os.environ.get("POWERCONTEXT_CLIENT_API_TOKEN", "")
            ),
            timeout_seconds=timeout_seconds,
            allow_insecure_http=bool(config.get("allow_insecure_http", False)),
        )
        logger.info(
            "[PowerContext] initialized: server=%s scope_mode=%s explicit_scope=%s auto_recall=%s capture=%s",
            self.client.server_url,
            self.scope_mode,
            bool(self.explicit_scope_id),
            self.auto_recall,
            self.capture_user_messages,
        )

    async def resolve_identity(
        self,
        *,
        bot_uuid: str,
        session_name: str,
        sender_id: str = "",
        sender_name: str = "",
    ) -> ResolvedPowerContext:
        identity = LangBotIdentity(
            bot_uuid=str(bot_uuid or ""),
            session_name=str(session_name or ""),
            sender_id=str(sender_id or ""),
            sender_name=str(sender_name or ""),
        )
        key = binding_key(identity, self.scope_mode)
        result = await self.client.resolve_scope(
            explicit_scope_id=self.explicit_scope_id or None,
            binding_keys=[key],
            allow_default=self.allow_default_scope,
        )
        scope_id = result.data.get("scope_id")
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("PowerContext returned no usable scope_id")
        return ResolvedPowerContext(
            scope_id=scope_id.strip(),
            identity=identity,
            key=key,
            request_id=result.request_id,
        )

    async def resolve_query(
        self,
        *,
        session: Any,
        query_id: int,
        query_uuid: str | None = None,
    ) -> ResolvedPowerContext:
        identity = await self.query_identity(
            session=session,
            query_id=query_id,
            query_uuid=query_uuid,
        )
        return await self.resolve_identity(
            bot_uuid=identity.bot_uuid,
            session_name=identity.session_name,
            sender_id=identity.sender_id,
            sender_name=identity.sender_name,
        )

    async def query_identity(
        self,
        *,
        session: Any,
        query_id: int,
        query_uuid: str | None = None,
    ) -> LangBotIdentity:
        api = QueryBasedAPIProxy(
            query_id=query_id,
            query_uuid=query_uuid,
            plugin_runtime_handler=self.plugin_runtime_handler,
        )
        bot_uuid = await api.get_bot_uuid()
        query_vars = await api.get_query_vars()
        return LangBotIdentity(
            bot_uuid=bot_uuid,
            session_name=session_name_from_session(session),
            sender_id=str(
                query_vars.get("sender_id", getattr(session, "sender_id", "")) or ""
            ),
            sender_name=str(query_vars.get("sender_name", "") or ""),
        )

    @staticmethod
    def source_id(
        *,
        query_uuid: str | None,
        query_id: int,
        session_name: str,
        content: str,
    ) -> str:
        stable_query = str(query_uuid or query_id)
        digest = hashlib.sha256(
            f"{session_name}\0{stable_query}\0{content}".encode("utf-8")
        ).hexdigest()
        return f"langbot-{digest}"
