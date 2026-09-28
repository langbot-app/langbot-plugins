"""Binding-scoped serialization for ambiguous Host mutations.

A cancelled caller waits for its dispatched operation while the lock remains
held. The SDK invocation capability is still authoritative at every Host call.
"""
import asyncio
from functools import wraps
from weakref import WeakValueDictionary


async def settle(task):
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
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
        getter = getattr(plugin, 'get_installation_binding', None)
        binding = getter() if getter else None
        # Dedicated mode is a single installation per component instance.
        return binding if binding is not None else ('dedicated',)

    async def run(self, plugin, operation):
        binding = self.binding(plugin)
        lock = self._locks.get(binding)
        if lock is None:
            lock = self._locks[binding] = asyncio.Lock()
        async with lock:
            if binding in self._fenced:
                raise RuntimeError('Installation state fenced after ambiguous Host mutation')
            try:
                # Never detach Host work from its revocable SDK invocation.
                return await operation()
            except BaseException:
                self._fenced.add(binding)
                raise

    def fence(self, plugin):
        self._fenced.add(self.binding(plugin))

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
