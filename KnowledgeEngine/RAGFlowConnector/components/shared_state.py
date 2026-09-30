"""Installation- and knowledge-base-scoped state using the SDK-bound Host proxy.

Bundled in each independent archive. No filesystem/global tenant registry.

Locks and fences are keyed by ``(installation binding, knowledge-base identity)``
so an ambiguous failure for one knowledge base never blocks the other knowledge
bases that share the same installation; the platform storage key is the same for
a whole installation, hence the knowledge-base id is always folded into our keys.
Fences are persisted in installation-bound Host storage (key includes the
knowledge-base id) with an in-process cache in front, so a restarted worker still
recognises them. An unreadable fence record refuses the operation instead of
being read as "no fence", and such a read is retried rather than cached. Only
dispatched mutations whose outcome is unknown fence; a deterministic validation
error raised before any remote call and ordinary cancellation before a call is
dispatched do not. A fence that cannot be persisted aborts the mutation before
it is dispatched, and a fence is released only after the provider answer was
confirmed and its Host record written.
"""
import asyncio
import hashlib
import inspect
import json
import logging
from functools import wraps
from contextlib import asynccontextmanager
import httpx
from weakref import WeakValueDictionary

logger = logging.getLogger(__name__)

HTTP_TOTAL_TIMEOUT = 150.0
FENCE_KEY_PREFIX = "ke.fence.v1."
# Fallback fence scope when the wrapped method carries no knowledge-base identity.
_INSTALLATION_FENCE_KEY = FENCE_KEY_PREFIX + "installation"
_IDENTITY_ARGUMENTS = ('kb_id', 'collection_id', 'knowledge_base_id')


class AmbiguousMutationError(RuntimeError):
    """A dispatched mutation's outcome is unknown and requires reconciliation.

    Raise this from a call site when a remote mutation may have taken effect but
    its result (or its durable record) is unknown. Only these failures fence a
    knowledge base; deterministic errors raised before any remote call, and
    ordinary cancellation before a call is dispatched, do not fence.
    """


class FenceStateUnavailableError(RuntimeError):
    """Persisted fence state could not be read, so the operation is refused.

    An unreadable fence record cannot be told apart from a knowledge base that
    is fenced after an ambiguous mutation, so an unknown fence state fails
    closed. The read is not cached: the next attempt re-reads the durable state,
    and nothing has been dispatched while the state stays unknown.
    """


class FenceLifecycleError(RuntimeError):
    """A mutation was aborted before dispatch because its fence is not in place.

    Both failures below mean "this call dispatched nothing": the pre-dispatch
    protection could not be persisted, or an earlier unresolved mutation is
    still fenced. Call sites must report them instead of folding them into an
    ordinary provider failure.
    """


class FencePersistError(FenceLifecycleError):
    """The pre-dispatch fence record could not be persisted.

    Raised by ``fence`` when the Host write of the fence record fails: the fence
    is not in place, so ``dispatched`` aborts before its request instead of
    dispatching a mutation whose protection does not exist.
    """


class FencedKnowledgeBaseError(FenceLifecycleError):
    """A mutation was refused: an earlier mutation's outcome is unresolved.

    Raised when a serialized operation, or a dispatch inside one, is attempted
    while the knowledge base (or the whole installation) is still fenced. A
    later successful operation therefore never clears an earlier unresolved
    operation's fence.
    """


def is_ambiguous_http_failure(exc):
    """Whether a failed provider call had an unknown outcome.

    Only a rejection the protocol makes unambiguous releases the caller's
    protection: a 4xx response means the provider received and refused the
    request, and a connection that was never established means nothing was
    dispatched. A 5xx or gateway response is NOT proof that the request was not
    applied — the provider or an intermediary may have committed the change and
    then failed — so it stays ambiguous together with timeouts, lost connections
    and unparsable responses.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = getattr(exc, "response", None)
        return response is None or response.status_code >= 500
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return False
    return True


async def _settle_commit(task):
    """Wait for a dispatched commit and report its outcome.

    Repeated caller cancellation never abandons the wait, so a Host commit that
    was already dispatched settles before its lock is released. Returns
    ``(caller_cancelled, failure)``; ``failure`` is the commit's exception, or
    ``asyncio.CancelledError`` when the commit itself was cancelled.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # The commit raised; its outcome is read from the state below.
            pass
    if task.cancelled():
        return cancelled, asyncio.CancelledError()
    return cancelled, task.exception()


def _kb_identity(method, instance, args, kwargs):
    """Extract the knowledge-base identity from a serialized method's arguments.

    Reads a ``kb_id`` / ``collection_id`` / ``knowledge_base_id`` argument first,
    then an argument exposing ``knowledge_base_id`` or ``get_collection_id()``
    (ingestion passes the whole context). The knowledge-base id is preferred: the
    collection id is a vector-store identity that need not equal it, and every
    other entry point of the connector is addressed by the knowledge-base id, so
    locking and fencing on the collection id would split one knowledge base into
    two independently protected scopes. Returns ``None`` when the wrapped method
    carries no knowledge-base identity, in which case the caller falls back to an
    installation-wide lock and fence.
    """
    try:
        bound = inspect.signature(method).bind_partial(instance, *args, **kwargs)
    except (TypeError, ValueError):
        return None
    for name in _IDENTITY_ARGUMENTS:
        value = bound.arguments.get(name)
        if isinstance(value, str) and value:
            return value
    for value in bound.arguments.values():
        identity = getattr(value, 'knowledge_base_id', None)
        if isinstance(identity, str) and identity:
            return identity
        getter = getattr(value, 'get_collection_id', None)
        if callable(getter):
            try:
                identity = getter()
            except Exception:
                continue
            if isinstance(identity, str) and identity:
                return identity
    return None


def _kb_fence_key(kb_identity):
    if kb_identity is None:
        return _INSTALLATION_FENCE_KEY
    return FENCE_KEY_PREFIX + hashlib.sha256(kb_identity.encode()).hexdigest()


class SerialState:
    def __init__(self):
        self._locks = WeakValueDictionary()
        # binding -> set of fence keys; cache in front of persisted fences.
        self._fenced = {}
        self._fence_loaded = set()
        # binding -> single-flight lock and mutation counter for the fence read.
        self._fence_load_locks: dict = {}
        self._fence_generation: dict = {}

    def _bump_fence_generation(self, binding):
        self._fence_generation[binding] = self._fence_generation.get(binding, 0) + 1

    @staticmethod
    def binding(plugin):
        try:
            binding = plugin.get_installation_binding()
        except Exception:
            # Revocation has no invocation context; fall back to dedicated scope.
            binding = None
        return binding if binding is not None else ('dedicated',)

    @staticmethod
    def _scope(binding, kb_identity):
        return (binding, kb_identity)

    def _fenced_keys(self, binding):
        return self._fenced.setdefault(binding, set())

    def is_fenced(self, binding, kb_identity):
        fenced = self._fenced_keys(binding)
        return (
            _kb_fence_key(kb_identity) in fenced
            or _INSTALLATION_FENCE_KEY in fenced
        )

    async def _load_fences(self, plugin, binding):
        """Read persisted fences once per binding, or refuse the operation.

        A failed read means the pending ambiguity of the installation is
        unknown, so it must not be mistaken for "no fences": the operation is
        refused with ``FenceStateUnavailableError`` and nothing is dispatched.
        The binding is not marked as loaded, so the next attempt re-reads and
        the knowledge base becomes usable again once storage answers. The read
        is single-flight per installation, and a snapshot read before a sibling
        operation changed the fence state is discarded instead of installed.
        """
        if binding in self._fence_loaded:
            return
        lock = self._fence_load_locks.get(binding)
        if lock is None:
            lock = self._fence_load_locks[binding] = asyncio.Lock()
        async with lock:
            if binding in self._fence_loaded:
                return
            for _ in range(8):
                generation = self._fence_generation.get(binding, 0)
                snapshot = await self._read_fences(plugin, binding)
                if generation == self._fence_generation.get(binding, 0):
                    self._fenced_keys(binding).update(snapshot)
                    self._fence_loaded.add(binding)
                    return
            raise FenceStateUnavailableError(
                f"Fence state for {binding!r} kept changing while it was being read"
            )

    async def _read_fences(self, plugin, binding) -> set:
        """Read the persisted fence keys for one installation.

        An unreadable marker counts as fenced and a failed read refuses the
        operation with ``FenceStateUnavailableError``; the read is never cached.
        """
        try:
            keys = await plugin.get_plugin_storage_keys()
            snapshot = set()
            for key in keys:
                if not key.startswith(FENCE_KEY_PREFIX):
                    continue
                try:
                    marker = json.loads(await plugin.get_plugin_storage(key))
                except Exception:
                    # Unreadable marker: fail closed on this knowledge base.
                    marker = {'reason': 'unreadable fence record'}
                if marker is not None:
                    snapshot.add(key)
            return snapshot
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Could not read persisted fences for %r", binding, exc_info=True)
            raise FenceStateUnavailableError(
                f"Could not read persisted fences for {binding!r}; pending ambiguous "
                f"mutations are unknown: {exc!r}"
            ) from exc

    async def run(self, plugin, kb_identity, operation, *, allow_fenced=False):
        binding = self.binding(plugin)
        key = self._scope(binding, kb_identity)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        async with lock:
            await self._load_fences(plugin, binding)
            if not allow_fenced and self.is_fenced(binding, kb_identity):
                raise FencedKnowledgeBaseError(
                    f"Knowledge base {kb_identity!r} fenced after an ambiguous mutation; "
                    "reconcile with the provider before retrying"
                )
            try:
                return await operation()
            except AmbiguousMutationError:
                await self.fence(plugin, kb_identity, 'ambiguous remote mutation')
                raise

    async def dispatched(self, plugin, kb_identity, operation, reason):
        """Dispatch one remote mutation under a pre-persisted fence.

        ``operation`` must cover the whole mutation: the provider request, the
        response validation and the durable result/mapping write. The fence is
        persisted before the request goes out — a failed persist aborts here,
        before any remote call — and it is released only once ``operation`` has
        returned, so an ambiguous outcome or a caller cancellation keeps the
        knowledge base fenced for reconciliation. A knowledge base that is
        already fenced is refused before anything is written or dispatched, so a
        later successful operation never clears an earlier unresolved
        operation's fence.
        """
        binding = self.binding(plugin)
        if self.is_fenced(binding, kb_identity):
            # The enclosing serialized call loaded and validated the fence state,
            # so this is a real unresolved mutation rather than a stale view.
            raise FencedKnowledgeBaseError(
                f"Knowledge base {kb_identity!r} fenced by an unresolved mutation; "
                "reconcile with the provider before dispatching another mutation"
            )
        await self.fence(plugin, kb_identity, reason)
        try:
            result = await operation()
        except asyncio.CancelledError:
            # Dispatched, outcome unknown: keep the fence and let the caller see it.
            raise
        except Exception as exc:
            if not is_ambiguous_http_failure(exc):
                await self.clear_fence(plugin, kb_identity)
            raise
        await self.clear_fence(plugin, kb_identity)
        return result

    async def fence(self, plugin, kb_identity, reason):
        """Persist one fence, then mark that knowledge base in this process.

        The durable record is written first and a failed write is fatal
        (``FencePersistError``), so a caller that fences before dispatching a
        mutation aborts before its request instead of dispatching under a fence
        that does not exist. Only a persisted fence sets the in-process marker,
        so a failed write and a caller cancellation both leave no marker behind.
        The residual race — the Host write landed but its acknowledgement was
        lost — can make a later read load a fence for an operation that was
        never dispatched; that is the conservative direction and is accepted.
        """
        binding = self.binding(plugin)
        fence_key = _kb_fence_key(kb_identity)
        payload = json.dumps({'kb_id': kb_identity, 'reason': reason}).encode()
        cancelled, failure = await _settle_commit(
            asyncio.create_task(plugin.set_plugin_storage(fence_key, payload))
        )
        if cancelled:
            raise asyncio.CancelledError
        if failure is not None:
            raise FencePersistError(
                f"Could not persist fence for {kb_identity!r}: {failure!r}"
            ) from failure
        self._fenced_keys(binding).add(fence_key)
        self._bump_fence_generation(binding)

    async def clear_fence(self, plugin, kb_identity):
        """Drop the fence for one knowledge base, in process and in storage.

        Used when a knowledge base is deleted: its pending ambiguity is moot, so
        the operator path out of a fence is deletion. A lingering persisted
        marker is inert because the configuration tombstone is already written.
        """
        binding = self.binding(plugin)
        fence_key = _kb_fence_key(kb_identity)
        self._fenced_keys(binding).discard(fence_key)
        self._bump_fence_generation(binding)
        cancelled, failure = await _settle_commit(
            asyncio.create_task(plugin.set_plugin_storage(fence_key, b'null'))
        )
        if failure is not None:
            logger.warning("Could not clear fence for %r: %r", kb_identity, failure)
        if cancelled:
            raise asyncio.CancelledError

    async def write(self, plugin, kb_identity, key, value):
        # A caller cancellation must not abandon a dispatched Host commit while
        # its lock is released; settle it and fence only a real ambiguity.
        task = asyncio.create_task(plugin.set_plugin_storage(key, value))
        cancelled, failure = await _settle_commit(task)
        if failure is not None:
            try:
                await self.fence(plugin, kb_identity, f'Host write outcome unknown: {failure!r}')
            except FencePersistError:
                # The write failure is what the caller must see; a fence that
                # could not be persisted is reported alongside it, not instead.
                logger.error(
                    "Could not persist fence for %r after a failed Host write",
                    kb_identity, exc_info=True,
                )
        if cancelled:
            raise asyncio.CancelledError
        if failure is not None:
            raise failure
        return task.result()

    def release_binding(self, binding):
        """Drop process-local state keyed by one installation binding."""
        for key in [k for k in self._locks if k[0] == binding]:
            self._locks.pop(key, None)
        self._fenced.pop(binding, None)
        self._fence_loaded.discard(binding)
        self._fence_load_locks.pop(binding, None)
        self._fence_generation.pop(binding, None)


def serialized(method=None, *, allow_fenced=False):
    def decorate(func):
        @wraps(func)
        async def call(self, *args, **kwargs):
            return await self._state.run(
                self.plugin,
                _kb_identity(func, self, args, kwargs),
                lambda: func(self, *args, **kwargs),
                allow_fenced=allow_fenced,
            )
        return call
    return decorate if method is None else decorate(method)


class ConfigStore:
    """No credential cache: every delete resolves durable installation storage.

    A JSON null tombstone makes missing KBs explicit without ambiguous delete RPCs.
    The one mutation lock also orders ingest/delete/create inside this worker.
    Configuration and document mappings are keyed by the Host knowledge-base id;
    records an earlier revision keyed by the ingestion collection id are resolved
    and rewritten under the canonical key by ``_load_document``.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._state = SerialState()
        self._http_slots = asyncio.Semaphore(4)

    async def initialize(self) -> None:
        await super().initialize()
        # The runtime dispatches installation revocation to the plugin instance,
        # which holds no back-reference to its components; hand it this
        # component's state so the revocation hook can drop per-binding locks and
        # fences. Without the hook (langbot-plugin 0.7.4) this attribute is simply
        # never read.
        plugin = getattr(self, 'plugin', None)
        if plugin is not None:
            plugin.knowledge_engine_serial_state = self._state

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
        await self._state.write(self.plugin, kb_id, self._key(kb_id), value)

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

    async def _legacy_document(self, keys, kb_id, host_document_id):
        """Find the mapping an earlier revision keyed by the collection id.

        An earlier revision of this connector keyed a knowledge base's durable
        records by the ingestion context's collection id, which need not equal
        the Host knowledge-base id every other entry point uses. Such a record
        names the identity it was written under, so the Host document id it
        records resolves it. More than one candidate means the document cannot be
        attributed without guesswork, so it is refused rather than picked.
        """
        matches = []
        for candidate in keys:
            if not candidate.startswith('ke.document.v1.'):
                continue
            item = json.loads(await self.plugin.get_plugin_storage(candidate))
            if not isinstance(item, dict):
                continue
            legacy_kb_id = item.get('kb_id')
            if (isinstance(legacy_kb_id, str) and legacy_kb_id and legacy_kb_id != kb_id
                    and item.get('host_document_id') == host_document_id):
                matches.append(item)
        if len(matches) > 1:
            raise ValueError('Ambiguous legacy document mapping; reconcile installation')
        return matches[0] if matches else None

    async def _migrate_legacy(self, kb_id, mapping):
        """Rewrite a collection-keyed document record under the canonical key.

        The mapping is rewritten canonically, and the configuration that the same
        legacy identity holds is copied across when the canonical configuration is
        missing, so a deletion that resolves the legacy mapping also finds the
        settings it needs to reach the upstream. A legacy identity that is absent
        leaves nothing to migrate.
        """
        await self._save_document(kb_id, mapping['host_document_id'], mapping)
        legacy_kb_id = mapping.get('kb_id')
        if not isinstance(legacy_kb_id, str) or not legacy_kb_id or legacy_kb_id == kb_id:
            return
        keys = await self.plugin.get_plugin_storage_keys()
        legacy_key = self._key(legacy_kb_id)
        if self._key(kb_id) in keys or legacy_key not in keys:
            return
        value = json.loads(await self.plugin.get_plugin_storage(legacy_key))
        if isinstance(value, dict):
            await self._save_config(kb_id, value)

    async def _load_document(self, kb_id, host_document_id):
        key = self._document_key(kb_id, host_document_id)
        keys = await self.plugin.get_plugin_storage_keys()
        if key in keys:
            value = json.loads(await self.plugin.get_plugin_storage(key))
        else:
            # A canonical miss may be a record an earlier revision wrote under the
            # collection key; resolving it migrates it (and the configuration it
            # shares that identity with) to the canonical key.
            legacy = await self._legacy_document(keys, kb_id, host_document_id)
            if legacy is not None:
                await self._migrate_legacy(kb_id, legacy)
                return {**legacy, 'kb_id': kb_id, 'host_document_id': host_document_id}
            # Current Host passes the durable upstream ID on delete; older Host
            # revisions pass its file UUID. Resolve either without trusting an
            # arbitrary unrecorded identifier.
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
        await self._state.write(self.plugin, kb_id, self._document_key(kb_id, host_document_id),
                                json.dumps(value).encode())
