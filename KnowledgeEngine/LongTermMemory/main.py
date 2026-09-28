from __future__ import annotations

import logging

from langbot_plugin.api.definition.plugin import BasePlugin

from store.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class LongTermMemoryPlugin(BasePlugin):

    memory_store: MemoryStore

    async def initialize(self) -> None:
        # Initialization is process-scoped; config is resolved at invocation time.
        self.memory_store = MemoryStore(plugin=self)
        logger.info("[LongTermMemory] plugin initialized")
