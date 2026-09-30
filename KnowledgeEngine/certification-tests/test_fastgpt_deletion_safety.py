"""FastGPT deletion safety: dispatched-but-unknown deletes, binding, and uploads.

Local HTTP fixture; real SDK objects via ``test_shared_state``'s loader.
"""
import asyncio

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
