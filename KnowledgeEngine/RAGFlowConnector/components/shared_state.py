"""Installation-local state using the SDK-bound Host proxy, never caller scope.

Bundled in each independent archive. No filesystem/global tenant registry.
"""
import asyncio
import hashlib
import json
from functools import wraps
from contextlib import asynccontextmanager
import httpx
from weakref import WeakValueDictionary

HTTP_TOTAL_TIMEOUT = 150.0


async def settle(task):
    """Do not release a lock while a dispatched Host commit can still finish."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve errors too; the operation itself has already fenced unsafe state.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class SerialState:
    def __init__(self):
        self._locks = WeakValueDictionary()
        self._fenced = set()

    @staticmethod
    def binding(plugin):
        binding = plugin.get_installation_binding()
        return binding if binding is not None else ('dedicated',)

    def fence(self, plugin):
        self._fenced.add(self.binding(plugin))

    async def run(self, plugin, operation):
        binding = self.binding(plugin)
        lock = self._locks.get(binding)
        if lock is None:
            lock = self._locks[binding] = asyncio.Lock()
        async with lock:
            if binding in self._fenced:
                raise RuntimeError("Installation state fenced after ambiguous storage failure; reconcile before restart")
            try:
                return await operation()
            except BaseException:
                self._fenced.add(binding)
                raise

    async def write(self, plugin, key, value):
        try:
            await plugin.set_plugin_storage(key, value)
        except BaseException:
            self.fence(plugin)
            raise


def serialized(method):
    @wraps(method)
    async def call(self, *args, **kwargs):
        return await self._state.run(self.plugin, lambda: method(self, *args, **kwargs))
    return call


class ConfigStore:
    """No credential cache: every delete resolves durable installation storage.

    A JSON null tombstone makes missing KBs explicit without ambiguous delete RPCs.
    The one mutation lock also orders ingest/delete/create inside this worker.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._state = SerialState()
        self._http_slots = asyncio.Semaphore(4)

    @asynccontextmanager
    async def http_client(self):
        # Include queueing and the whole response body in the deadline, not just
        # individual socket reads. Clients/credentials never survive a call.
        async with asyncio.timeout(HTTP_TOTAL_TIMEOUT):
            async with self._http_slots:
                async with httpx.AsyncClient() as client:
                    yield client

    @staticmethod
    def _key(kb_id):
        return "ke.config.v1." + hashlib.sha256(kb_id.encode()).hexdigest()

    async def _save_config(self, kb_id, config):
        value = json.dumps(config, ensure_ascii=False).encode()
        if len(value) > 65536:
            raise ValueError("Knowledge-base configuration exceeds 64 KiB")
        await self._state.write(self.plugin, self._key(kb_id), value)

    async def _load_config(self, kb_id):
        key = self._key(kb_id)
        # Only an authoritative keys response establishes absence; RPC errors propagate.
        if key not in await self.plugin.get_plugin_storage_keys():
            return None
        value = json.loads(await self.plugin.get_plugin_storage(key))
        if value is not None and not isinstance(value, dict):
            raise ValueError("Invalid persisted knowledge-base configuration")
        return value

    @staticmethod
    def _document_key(kb_id, host_document_id):
        return "ke.document.v1." + hashlib.sha256(
            json.dumps([kb_id, host_document_id]).encode()
        ).hexdigest()

    async def _load_document(self, kb_id, host_document_id):
        key = self._document_key(kb_id, host_document_id)
        keys = await self.plugin.get_plugin_storage_keys()
        if key in keys:
            value = json.loads(await self.plugin.get_plugin_storage(key))
        else:
            matches = []
            for candidate in keys:
                if candidate.startswith('ke.document.v1.'):
                    item = json.loads(await self.plugin.get_plugin_storage(candidate))
                    if item.get('kb_id') == kb_id and item.get('upstream_id') == host_document_id:
                        matches.append(item)
            if len(matches) > 1:
                raise ValueError('Ambiguous upstream document ID')
            return matches[0] if matches else None
        if not isinstance(value, dict) or not isinstance(value.get('upstream_id'), str):
            raise ValueError('Invalid upstream document mapping; reconcile installation')
        return value

    async def _save_document(self, kb_id, host_document_id, value):
        value = {**value, 'kb_id': kb_id, 'host_document_id': host_document_id}
        await self._state.write(self.plugin, self._document_key(kb_id, host_document_id),
                                json.dumps(value).encode())
