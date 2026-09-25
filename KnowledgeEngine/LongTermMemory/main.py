from __future__ import annotations

import logging

from langbot_plugin.api.definition.plugin import BasePlugin

from store.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class LongTermMemoryPlugin(BasePlugin):

    memory_store: MemoryStore

    async def initialize(self) -> None:
        config = self.get_config()
        self.memory_store = MemoryStore(
            plugin=self,
            max_profile_traits=config.get("max_profile_traits", 20),
            max_profile_preferences=config.get("max_profile_preferences", 10),
        )
        logger.info(
            "[LongTermMemory] plugin initialized: max_profile_traits=%s max_profile_preferences=%s",
            config.get("max_profile_traits", 20),
            config.get("max_profile_preferences", 10),
        )
