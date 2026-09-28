"""Per-installation storage transaction lock; no tenant data on shared instances."""
import asyncio
from functools import wraps
from weakref import WeakValueDictionary


class BindingWrites:
    def __init__(self):
        self._locks = WeakValueDictionary()
        self._fenced = set()
        self._owners = {}

    @staticmethod
    def binding(plugin):
        getter = getattr(plugin, 'get_installation_binding', None)
        binding = getter() if getter else None
        return binding if binding is not None else ('dedicated',)

    def fence(self, plugin):
        self._fenced.add(self.binding(plugin))

    async def run(self, plugin, operation):
        binding = self.binding(plugin)
        task = asyncio.current_task()
        if self._owners.get(binding) is task:
            return await operation()
        lock = self._locks.get(binding)
        if lock is None:
            lock = self._locks[binding] = asyncio.Lock()
        async with lock:
            if binding in self._fenced:
                raise RuntimeError('Installation writes fenced after ambiguous Host mutation')
            self._owners[binding] = task
            try:
                return await operation()
            except BaseException:
                self._fenced.add(binding)
                raise
            finally:
                self._owners.pop(binding, None)


def serialized_write(method):
    @wraps(method)
    async def call(self, *args, **kwargs):
        return await self._writes.run(self.plugin, lambda: method(self, *args, **kwargs))
    return call
