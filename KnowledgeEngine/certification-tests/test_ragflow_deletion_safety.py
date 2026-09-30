"""RAGFlow connector: fail-closed fences and upstream-bound document mappings.

Host storage and the provider are local fixtures; no live deployment is
contacted. The behaviour asserted here is scoped to RAGFlow, so it lives in its
own file rather than in the shared per-connector fixtures.
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

FENCE_KEY_PREFIX = 'ke.fence.v1.'


def fence_records(store):
    """Persisted fence markers that still require reconciliation."""
    return [json.loads(value) for key, value in store.data.items()
            if key.startswith(FENCE_KEY_PREFIX) and json.loads(value) is not None]


def provider(monkeypatch, handler):
    """Route the connector's HTTP through a recording mock transport."""
    calls = []

    async def service(request):
        calls.append(request)
        response = handler(request)
        if asyncio.iscoroutine(response):
            response = await response
        return response

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, 'AsyncClient',
        lambda **kw: client(transport=httpx.MockTransport(service), **kw),
    )
    return calls


def acknowledge(request):
    if request.method == 'POST' and request.url.path.endswith('/documents'):
        return httpx.Response(200, json={'code': 0, 'data': [{'id': 'upstream-1'}]})
    if request.method == 'DELETE':
        return httpx.Response(200, json={'code': 0})
    return httpx.Response(200, json={'code': 0, 'data': []})


def engine_with(monkeypatch, store, handler):
    calls = provider(monkeypatch, handler)
    pc, ec = load_plugin('RAGFlowConnector')
    engine = ec()
    plugin = bind(pc, store)

    async def read_file(path):
        return b'fixture file'

    plugin.get_knowledge_file_stream = read_file
    engine.plugin = plugin
    return engine, calls


def created_mapping(upstream_target, dataset_id='A'):
    return {'upstream_id': 'upstream-1', 'dataset_id': dataset_id,
            'api_base_url': upstream_target, 'status': 'created'}


@pytest.mark.asyncio
async def test_unreadable_fence_state_refuses_without_dispatch_and_recovers(monkeypatch):
    store = StorageFixture()
    engine, calls = engine_with(monkeypatch, store, acknowledge)
    await engine._save_config('kb', configuration('A'))
    await engine._save_document('kb', 'doc', created_mapping('https://fixture.invalid'))

    store.fail = True
    # An unreadable fence record is not the same as "no fences": the mutation is
    # refused before anything is dispatched.
    with pytest.raises(RuntimeError, match='Could not read persisted fences'):
        await engine.delete_document('kb', 'doc')
    assert calls == []

    store.fail = False
    # The failed read was not cached, so the next attempt re-reads and proceeds.
    assert await engine.delete_document('kb', 'doc') is True
    assert [request.method for request in calls] == ['DELETE']


@pytest.mark.asyncio
async def test_cancelled_upload_dispatch_fences_and_keeps_the_pending_mapping(monkeypatch):
    dispatched = asyncio.Event()

    async def handler(request):
        if request.method == 'POST' and request.url.path.endswith('/documents'):
            dispatched.set()
            await asyncio.sleep(30)
        return acknowledge(request)

    store = StorageFixture()
    engine, calls = engine_with(monkeypatch, store, handler)
    task = asyncio.create_task(engine.ingest(ingest_context(configuration('A'))))
    await asyncio.wait_for(dispatched.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The upload may have committed upstream, so the knowledge base stays fenced
    # until an operator reconciles it, in process and in durable storage.
    assert engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
    assert [record['kb_id'] for record in fence_records(store)] == ['kb']
    assert [request.method for request in calls] == ['POST']
    mapping = await engine._load_document('kb', 'local-doc')
    assert mapping['status'] == 'pending', 'a cancelled upload must not advance the mapping'
    assert mapping['upstream_id'] == ''


@pytest.mark.asyncio
async def test_cancelled_delete_dispatch_fences_and_keeps_the_created_mapping(monkeypatch):
    dispatched = asyncio.Event()

    async def handler(request):
        if request.method == 'DELETE':
            dispatched.set()
            await asyncio.sleep(30)
        return acknowledge(request)

    store = StorageFixture()
    engine, calls = engine_with(monkeypatch, store, handler)
    await engine._save_config('kb', configuration('A'))
    await engine._save_document('kb', 'doc', created_mapping('https://fixture.invalid'))

    task = asyncio.create_task(engine.delete_document('kb', 'doc'))
    await asyncio.wait_for(dispatched.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
    assert [record['kb_id'] for record in fence_records(store)] == ['kb']
    assert [request.method for request in calls] == ['DELETE']
    mapping = await engine._load_document('kb', 'doc')
    assert mapping['status'] == 'created', 'an unacknowledged delete must not tombstone the mapping'


@pytest.mark.asyncio
async def test_deterministic_upload_rejection_leaves_no_fence(monkeypatch):
    async def handler(request):
        if request.method == 'POST' and request.url.path.endswith('/documents'):
            return httpx.Response(500, text='rejected')
        return acknowledge(request)

    store = StorageFixture()
    engine, calls = engine_with(monkeypatch, store, handler)
    result = await engine.ingest(ingest_context(configuration('A')))
    assert result.status == DocumentStatus.FAILED

    # The provider answered, so the pre-dispatch intent was cleared again and the
    # knowledge base remains usable.
    assert not engine._state.is_fenced(engine._state.binding(engine.plugin), 'kb')
    assert fence_records(store) == []
    await engine.on_knowledge_base_create('kb', configuration('A'))


@pytest.mark.asyncio
async def test_mapping_of_another_upstream_is_refused_before_dispatch(monkeypatch):
    store = StorageFixture()
    engine, calls = engine_with(monkeypatch, store, acknowledge)
    await engine._save_config('kb', configuration('A'))
    await engine._save_document('kb', 'doc', created_mapping('https://other.invalid'))

    with pytest.raises(RuntimeError, match='Upstream target changed'):
        await engine.delete_document('kb', 'doc')
    assert calls == []


@pytest.mark.asyncio
async def test_mapping_without_a_recorded_upstream_is_refused_before_dispatch(monkeypatch):
    store = StorageFixture()
    engine, calls = engine_with(monkeypatch, store, acknowledge)
    await engine._save_config('kb', configuration('A'))
    # Historical mapping, written before the upstream target was recorded.
    await engine._save_document('kb', 'doc', {'upstream_id': 'upstream-1', 'dataset_id': 'A',
                                              'status': 'created'})

    with pytest.raises(RuntimeError, match='Upstream target changed'):
        await engine.delete_document('kb', 'doc')
    assert calls == []


@pytest.mark.asyncio
async def test_equivalent_configuration_of_the_recorded_upstream_is_accepted(monkeypatch):
    store = StorageFixture()
    engine, calls = engine_with(monkeypatch, store, acknowledge)
    config = configuration('A')
    result = await engine.ingest(ingest_context(config))
    assert result.status == DocumentStatus.PROCESSING
    mapping = await engine._load_document('kb', 'local-doc')
    assert mapping['api_base_url'] == 'https://fixture.invalid', 'the canonical target is recorded'

    # Casing, a default port and a trailing slash never distinguish deployments.
    await engine._save_config('kb', {**config, 'api_base_url': 'HTTPS://FIXTURE.INVALID:443/'})
    assert await engine.delete_document('kb', 'local-doc') is True
    assert [request.method for request in calls] == ['POST', 'POST', 'DELETE']
