"""FastGPT deletion safety: dispatched-but-unknown deletes, binding, and uploads.

Local HTTP fixture; real SDK objects via ``test_shared_state``'s loader.
"""
import asyncio
import json

import httpx
import pytest
from test_shared_state import (
    StorageFixture,
    bind,
    configuration,
    ingest_context,
    load_plugin,
)

from langbot_plugin.api.entities.builtin.rag import DocumentStatus


def bind_mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, 'AsyncClient',
        lambda **kw: client(transport=httpx.MockTransport(handler), **kw),
    )


def upload_engine(pc, ec, store):
    """An engine whose Host resolves storage and file streaming to local fixtures."""
    engine = ec()
    plugin = bind(pc, store)

    async def read_file(path):
        return b'fixture file'

    plugin.get_knowledge_file_stream = read_file
    engine.plugin = plugin
    return engine


@pytest.mark.asyncio
async def test_fastgpt_cancelled_delete_keeps_fence_and_mapping(monkeypatch):
    pc, ec = load_plugin('FastGPTConnector')
    dispatched = asyncio.Event()
    release = asyncio.Event()

    async def service(request):
        if request.method == 'DELETE':
            dispatched.set()
            await release.wait()
        return httpx.Response(200, json={'code': 200, 'data': None})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = ec()
    engine.plugin = bind(pc, store)
    cfg = configuration('A')
    await engine.on_knowledge_base_create('kb', cfg)
    await engine._save_document('kb', 'host-doc', {
        'upstream_id': 'upstream-doc', 'dataset_id': 'A', 'status': 'created',
        'api_base_url': cfg['api_base_url']})
    task = asyncio.create_task(engine.delete_document('kb', 'host-doc'))
    await dispatched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    # The delete was already dispatched, so its outcome is unknown: the mapping
    # must not be closed out as deleted.
    assert (await engine._load_document('kb', 'host-doc'))['status'] == 'created'
    # The knowledge base stays fenced, also for a worker that restarts.
    with pytest.raises(RuntimeError, match='fenced'):
        await engine.delete_document('kb', 'host-doc')
    restarted = ec()
    restarted.plugin = bind(pc, store)
    with pytest.raises(RuntimeError, match='fenced'):
        await restarted.delete_document('kb', 'host-doc')


@pytest.mark.asyncio
@pytest.mark.parametrize('recorded_target', [None, 'https://other.invalid'])
async def test_fastgpt_delete_refuses_mapping_from_another_upstream(
    monkeypatch, recorded_target
):
    pc, ec = load_plugin('FastGPTConnector')
    calls = []

    async def service(request):
        calls.append(request.method)
        return httpx.Response(200, json={'code': 200, 'data': None})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = ec()
    engine.plugin = bind(pc, store)
    cfg = configuration('A')
    await engine.on_knowledge_base_create('kb', cfg)
    mapping = {'upstream_id': 'upstream-doc', 'dataset_id': 'A', 'status': 'created'}
    if recorded_target is not None:
        mapping['api_base_url'] = recorded_target
    await engine._save_document('kb', 'host-doc', mapping)
    # A mapping that is not bound to the configured upstream target (including a
    # mapping recorded before targets were bound) is refused before any request.
    with pytest.raises(RuntimeError, match='Upstream target changed'):
        await engine.delete_document('kb', 'host-doc')
    assert calls == []


@pytest.mark.asyncio
async def test_fastgpt_deterministic_upload_rejection_leaves_no_fence(monkeypatch):
    pc, ec = load_plugin('FastGPTConnector')

    async def service(request):
        return httpx.Response(400, json={'message': 'unsupported file'})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = upload_engine(pc, ec, store)
    result = await engine.ingest(ingest_context(configuration('A')))
    assert result.status == DocumentStatus.FAILED
    # The provider answered, so the pre-dispatch upload intent is cleared again:
    # a restarted worker accepts mutations for this knowledge base.
    restarted = upload_engine(pc, ec, store)
    await restarted.on_knowledge_base_create('kb', configuration('A'))


@pytest.mark.asyncio
async def test_fastgpt_upload_binds_mapping_to_normalized_upstream_target(monkeypatch):
    pc, ec = load_plugin('FastGPTConnector')

    async def service(request):
        if request.method == 'POST':
            return httpx.Response(200, json={
                'code': 200, 'data': {'collectionId': 'upstream-doc', 'results': {}},
            })
        return httpx.Response(200, json={'code': 200, 'data': None})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = upload_engine(pc, ec, store)
    cfg = configuration('A')
    # Casing and a trailing slash are config spellings, not a different upstream.
    upload = await engine.ingest(ingest_context({
        **cfg, 'api_base_url': cfg['api_base_url'].upper() + '/',
    }))
    assert upload.status == DocumentStatus.PROCESSING
    mapping = await engine._load_document('kb', 'local-doc')
    assert mapping['api_base_url'] == cfg['api_base_url']
    # The mapping names the upstream that owns the collection, so a delete
    # against that same upstream still succeeds.
    assert await engine.delete_document('kb', 'local-doc') is True


def fence_key_fault(shared_state, message):
    def fault(key):
        return RuntimeError(message) if key.startswith(shared_state.FENCE_KEY_PREFIX) else None
    return fault


@pytest.mark.asyncio
async def test_fastgpt_unpersistable_fence_aborts_the_upload_before_the_request(monkeypatch):
    pc, ec = load_plugin('FastGPTConnector')
    from components import shared_state

    calls = []

    async def service(request):
        calls.append(request)
        return httpx.Response(200, json={
            'code': 200, 'data': {'collectionId': 'upstream-doc', 'results': {}},
        })

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    store.write_fault = fence_key_fault(shared_state, 'fixture fence write rejected')
    engine = upload_engine(pc, ec, store)

    # The pre-written protection cannot be persisted, so the upload must not be
    # dispatched at all.
    with pytest.raises(shared_state.FencePersistError, match='Could not persist fence'):
        await engine.ingest(ingest_context(configuration('A')))
    assert calls == []
    # A mutation that was never dispatched leaves no fence behind...
    assert not engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
    assert [key for key in store.data if key.startswith(shared_state.FENCE_KEY_PREFIX)] == []
    # ...only the durable pre-dispatch intent it had already recorded.
    assert (await engine._load_document('kb', 'local-doc'))['status'] == 'pending'


@pytest.mark.asyncio
async def test_fastgpt_unpersistable_fence_aborts_the_delete_before_the_request(monkeypatch):
    pc, ec = load_plugin('FastGPTConnector')
    from components import shared_state

    calls = []

    async def service(request):
        calls.append(request)
        return httpx.Response(200, json={'code': 200, 'data': None})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = ec()
    engine.plugin = bind(pc, store)
    cfg = configuration('A')
    await engine.on_knowledge_base_create('kb', cfg)
    await engine._save_document('kb', 'host-doc', {
        'upstream_id': 'upstream-doc', 'dataset_id': 'A', 'status': 'created',
        'api_base_url': cfg['api_base_url']})

    store.write_fault = fence_key_fault(shared_state, 'fixture fence write rejected')
    with pytest.raises(shared_state.FencePersistError, match='Could not persist fence'):
        await engine.delete_document('kb', 'host-doc')
    assert calls == []
    assert not engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
    assert [key for key in store.data if key.startswith(shared_state.FENCE_KEY_PREFIX)] == []
    # The delete was never dispatched, so its mapping is still the recorded one.
    assert (await engine._load_document('kb', 'host-doc'))['status'] == 'created'


@pytest.mark.asyncio
async def test_fastgpt_cancellation_after_ack_before_the_mapping_keeps_the_fence(monkeypatch):
    pc, ec = load_plugin('FastGPTConnector')

    calls = []

    async def service(request):
        calls.append(request)
        return httpx.Response(200, json={'code': 200, 'data': None})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = ec()
    engine.plugin = bind(pc, store)
    cfg = configuration('A')
    await engine.on_knowledge_base_create('kb', cfg)
    await engine._save_document('kb', 'host-doc', {
        'upstream_id': 'upstream-doc', 'dataset_id': 'A', 'status': 'created',
        'api_base_url': cfg['api_base_url']})

    # The documented window: the provider has acknowledged the delete, the Host
    # record of it has not been written yet.
    entered = asyncio.Event()
    release = asyncio.Event()
    record = engine._save_document

    async def gated_record(kb_id, host_document_id, value):
        if value.get('status') == 'deleted':
            entered.set()
            await release.wait()
        return await record(kb_id, host_document_id, value)

    monkeypatch.setattr(engine, '_save_document', gated_record)
    task = asyncio.create_task(engine.delete_document('kb', 'host-doc'))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()

    # The delete was acknowledged upstream but its Host record does not exist, so
    # the protection must not have been released: a retry would replay the delete.
    assert (await engine._load_document('kb', 'host-doc'))['status'] == 'created'
    assert engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
    with pytest.raises(RuntimeError, match='fenced'):
        await engine.delete_document('kb', 'host-doc')
    assert [request.method for request in calls] == ['DELETE']


@pytest.mark.asyncio
async def test_fastgpt_later_success_never_clears_an_unresolved_fence():
    pc, ec = load_plugin('FastGPTConnector')
    from components import shared_state

    store = StorageFixture()
    engine = ec()
    engine.plugin = bind(pc, store)
    plugin = engine.plugin
    await engine._state.fence(plugin, 'kb', 'earlier delete outcome unknown')

    ran = []

    async def later_mutation():
        ran.append(1)
        return 'dispatched'

    # The later mutation is refused instead of running and releasing the earlier
    # operation's fence as if its own success had resolved it.
    with pytest.raises(shared_state.FencedKnowledgeBaseError, match='unresolved'):
        await engine._state.dispatched(plugin, 'kb', later_mutation, 'later mutation')
    assert ran == []
    assert engine._state.is_fenced(engine._state.binding(plugin), 'kb')
    assert [key for key in store.data if key.startswith(shared_state.FENCE_KEY_PREFIX)]


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [500, 400])
async def test_fastgpt_delete_outcome_is_unknown_exactly_on_a_server_error(monkeypatch, status):
    pc, ec = load_plugin('FastGPTConnector')
    from components import shared_state

    calls = []

    async def service(request):
        calls.append(request.method)
        return httpx.Response(status, json={'message': 'upstream failed'})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = ec()
    engine.plugin = bind(pc, store)
    cfg = configuration('A')
    await engine.on_knowledge_base_create('kb', cfg)
    await engine._save_document('kb', 'host-doc', {
        'upstream_id': 'upstream-doc', 'dataset_id': 'A', 'status': 'created',
        'api_base_url': cfg['api_base_url']})

    if status >= 500:
        # The provider or a gateway failed after receiving the delete, so whether
        # it was applied is unknown: the fence stays and the caller is told
        # instead of being handed a resolved rejection.
        with pytest.raises(shared_state.AmbiguousMutationError):
            await engine.delete_document('kb', 'host-doc')
        assert engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
        # The fence is durable, not just in this process.
        assert json.loads(store.data[shared_state._kb_fence_key('kb')]) is not None
        # Nothing was recorded as deleted, and the next mutation for this
        # knowledge base is refused, also for a worker that restarts.
        assert (await engine._load_document('kb', 'host-doc'))['status'] == 'created'
        with pytest.raises(RuntimeError, match='fenced'):
            await engine.delete_document('kb', 'host-doc')
        restarted = ec()
        restarted.plugin = bind(pc, store)
        with pytest.raises(RuntimeError, match='fenced'):
            await restarted.delete_document('kb', 'host-doc')
    else:
        # The provider answered with a rejection, so the delete did not happen.
        assert await engine.delete_document('kb', 'host-doc') is False
        assert not engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
        assert json.loads(store.data[shared_state._kb_fence_key('kb')]) is None
        restarted = ec()
        restarted.plugin = bind(pc, store)
        assert await restarted.delete_document('kb', 'host-doc') is False


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [500, 400])
async def test_fastgpt_upload_outcome_is_unknown_exactly_on_a_server_error(monkeypatch, status):
    pc, ec = load_plugin('FastGPTConnector')
    from components import shared_state

    async def service(request):
        return httpx.Response(status, json={'message': 'upstream failed'})

    bind_mock_http(monkeypatch, service)
    store = StorageFixture()
    engine = upload_engine(pc, ec, store)
    cfg = configuration('A')

    if status >= 500:
        # A dispatched upload whose answer is a server error has an unknown
        # outcome, so it is neither reported as a plain failure nor unfenced.
        with pytest.raises(shared_state.AmbiguousMutationError):
            await engine.ingest(ingest_context(cfg))
        assert engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
        assert json.loads(store.data[shared_state._kb_fence_key('kb')]) is not None
        assert (await engine._load_document('kb', 'local-doc'))['status'] == 'pending'
        with pytest.raises(RuntimeError, match='fenced'):
            await engine.on_knowledge_base_create('kb', cfg)
        restarted = upload_engine(pc, ec, store)
        with pytest.raises(RuntimeError, match='fenced'):
            await restarted.on_knowledge_base_create('kb', cfg)
    else:
        # The provider answered with a rejection, so the upload is a failure with
        # a resolved outcome and the knowledge base stays usable.
        result = await engine.ingest(ingest_context(cfg))
        assert result.status == DocumentStatus.FAILED
        assert not engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
        await engine.on_knowledge_base_create('kb', cfg)


@pytest.mark.asyncio
async def test_fastgpt_concurrent_first_load_cannot_resurrect_a_cleared_fence(monkeypatch):
    pc, ec = load_plugin('FastGPTConnector')
    from components import shared_state

    store = StorageFixture()
    seed = ec()
    seed.plugin = bind(pc, store)
    # An earlier ambiguous mutation left this knowledge base durably fenced.
    await seed._state.fence(seed.plugin, 'cleared', 'earlier delete outcome unknown')

    # The restarted worker's first fence load blocks on that record, so the
    # delete of the fenced knowledge base is issued while the read is in flight.
    store.gate_read(shared_state._kb_fence_key('cleared'))
    engine = ec()
    engine.plugin = bind(pc, store)
    cfg = configuration('A')
    loading = asyncio.create_task(engine.on_knowledge_base_create('fresh', cfg))
    await asyncio.wait_for(store.gate_entered.wait(), 5)
    clearing = asyncio.create_task(engine.on_knowledge_base_delete('cleared'))
    for _ in range(200):
        await asyncio.sleep(0)
    # Both first calls share one Host read instead of one read per knowledge base.
    assert store.keys_reads == 1
    store.gate_release.set()
    await loading
    await clearing

    assert store.keys_reads == 1
    # The cleared knowledge base is usable: the snapshot that was read before
    # the clear was not installed in its place.
    assert not engine._state.is_fenced(engine._state.binding(engine.plugin), 'cleared')
    await engine.on_knowledge_base_create('cleared', cfg)


@pytest.mark.asyncio
async def test_fastgpt_load_discards_a_snapshot_read_before_a_sibling_clear():
    pc, ec = load_plugin('FastGPTConnector')
    from components import shared_state

    store = StorageFixture()
    seed = ec()
    seed.plugin = bind(pc, store)
    await seed._state.fence(seed.plugin, 'kb', 'earlier delete outcome unknown')

    store.gate_read(shared_state._kb_fence_key('kb'))
    engine = ec()
    engine.plugin = bind(pc, store)
    cfg = configuration('A')
    loading = asyncio.create_task(engine.on_knowledge_base_create('kb', cfg))
    await asyncio.wait_for(store.gate_entered.wait(), 5)
    # The operator path out of a fence lands while the load's Host read is in
    # flight and already holds the fence record in hand.
    await engine._state.clear_fence(engine.plugin, 'kb')
    store.gate_release.set()
    await loading

    # The stale snapshot was discarded and re-read instead of installed: the
    # cleared knowledge base neither fails this call nor stays fenced.
    assert not engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
    assert store.keys_reads == 2
    await engine.on_knowledge_base_create('kb', configuration('B'))
