"""SDK 0.7.4 real child, WebSocket RPC, two slots and one component graph.

Local Host vector/storage fixture only; no provider/Cloud/sandbox claim.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys

import websockets
from langbot_plugin.utils.discover.engine import ComponentDiscoveryEngine
from langbot_plugin.cli.utils.page_components import discover_plugin_components
from langbot_plugin.cli.run.controller import PluginRuntimeController
from langbot_plugin.cli.run.handler import PluginRuntimeHandler
from langbot_plugin.runtime.io.handler import Handler, ActionResponse
from langbot_plugin.runtime.io.connections.ws import WebSocketConnection
from langbot_plugin.entities.io.actions.enums import RuntimeToPluginAction as R, PluginToRuntimeAction as P
from langbot_plugin.entities.io.context import InstallationBinding

CODE = Path(sys.argv[1]).resolve()
os.chdir(CODE)
sys.path.insert(0, str(CODE))

def binding(label):
    return InstallationBinding(instance_uuid='fixture-instance', workspace_uuid='workspace-'+label,
        placement_generation=1, installation_uuid='installation-'+label,
        runtime_revision=1, artifact_digest='a'*64)

async def child(url):
    discovery = ComponentDiscoveryEngine()
    manifest = discovery.load_component_manifest('manifest.yaml', no_save=True)
    # Probe shared eligibility without changing the dedicated release manifest.
    if CODE.name == 'LongTermMemory':
        manifest.execution.shared_runtime = 'shared-runtime-v1'
        manifest.execution.component_model = 'stateless-v1'
    components = discover_plugin_components(manifest, discovery)
    controller = PluginRuntimeController(manifest, components, False, '')
    original_attach = controller.initialize_slot
    identities = []
    async def attach(binding, settings):
        slot = await original_attach(binding, settings)
        identity = (id(slot.plugin_container.plugin_instance),
                    tuple(id(c.component_instance) for c in slot.plugin_container.components))
        identities.append(identity)
        if len(identities) == 2:
            assert identities[0] == identities[1], 'Slots have different object graphs'
            print('SAME_OBJECT_GRAPH=true', file=sys.stderr, flush=True)
        return slot
    async with websockets.connect(url) as ws:
        handler = PluginRuntimeHandler(WebSocketConnection(ws), controller.initialize)
        handler.plugin_container = controller.plugin_container
        handler._slot_initialize_callback = attach
        handler._slot_detach_callback = controller.detach_slot
        handler._slot_cancel_callback = controller.invalidate_slot
        controller.handler = handler
        await handler.run()

async def parent():
    stores = {label: {} for label in 'AB'}
    vectors = {label: {} for label in 'AB'}
    actions = []
    connected = asyncio.get_running_loop().create_future()
    async def accept(ws):
        handler = Handler(WebSocketConnection(ws), file_storage_dir=Path('/tmp/ke-shared-probe-host'))
        def label():
            ctx = handler.current_action_context
            assert ctx in (binding('A'), binding('B')), ctx
            return ctx.workspace_uuid[-1]
        @handler.action(P.GET_PLUGIN_STORAGE_KEYS)
        async def keys(data):
            l = label(); actions.append((l, 'keys'))
            return ActionResponse.success({'keys': list(stores[l])})
        @handler.action(P.GET_PLUGIN_STORAGE)
        async def get(data):
            l = label(); actions.append((l, 'get'))
            if data['key'] not in stores[l]:
                stores[l][data['key']] = base64.b64encode(b'{}').decode()
            return ActionResponse.success({'value_base64': stores[l][data['key']]})
        @handler.action(P.SET_PLUGIN_STORAGE)
        async def set_value(data):
            l = label(); actions.append((l, 'set'))
            stores[l][data['key']] = data['value_base64']
            return ActionResponse.success({})
        @handler.action(P.INVOKE_EMBEDDING)
        async def embed(data):
            l = label(); actions.append((l, 'embed'))
            return ActionResponse.success({'vectors': [[1.0] for _ in data['texts']]})
        @handler.action(P.VECTOR_UPSERT)
        async def upsert(data):
            l = label(); actions.append((l, 'upsert'))
            for id_, meta in zip(data['ids'], data['metadata']):
                vectors[l][id_] = {'id': id_, 'distance': .1, 'metadata': meta}
            return ActionResponse.success({})
        @handler.action(P.VECTOR_SEARCH)
        async def search(data):
            l = label(); actions.append((l, 'search'))
            return ActionResponse.success({'results': list(vectors[l].values())[:data['top_k']]})
        @handler.action(P.VECTOR_DELETE)
        async def delete(data):
            l = label(); actions.append((l, 'delete'))
            n = len(vectors[l]); vectors[l].clear()
            return ActionResponse.success({'count': n})
        connected.set_result(handler)
        await handler.run()
    async with websockets.serve(accept, '127.0.0.1', 0) as server:
        url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        proc = await asyncio.create_subprocess_exec(sys.executable, __file__, str(CODE), '--child', url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            host = await asyncio.wait_for(connected, 15)
            async def call(label, action, data):
                return await host.call_action(action, data, action_context=binding(label), timeout=20)
            settings = lambda label: {'enabled': True, 'priority': 0, 'plugin_config': {'max_profile_traits': 1 if label == 'A' else 4, 'max_profile_preferences': 1 if label == 'A' else 4}}
            for label in 'AB':
                await call(label, R.ATTACH_PLUGIN_SLOT, {'plugin_settings': settings(label)})
            a = await call('A', R.GET_PLUGIN_SLOT_CONTAINER, {})
            b = await call('B', R.GET_PLUGIN_SLOT_CONTAINER, {})
            assert a['plugin_config'] != b['plugin_config']
            if CODE.name == 'LangRAG':
                for label in 'AB':
                    result = await call(label, R.INGEST_DOCUMENT, {'context': {
                        'knowledge_base_id': 'same-kb', 'creation_settings': {'embedding_model_uuid': 'fixture-embedding'},
                        'file_object': {'metadata': {'filename': 'doc.txt', 'document_id': 'same-doc',
                            'knowledge_base_id': 'same-kb', 'file_size': 12, 'mime_type': 'text/plain'}, 'storage_path': 'fixture-file'},
                        'parsed_content': {'text': 'private-'+label}}})
                    assert result['status'] == 'completed', result
                for label in 'AB':
                    result = await call(label, R.RETRIEVE_KNOWLEDGE, {'retriever_name': '', 'retrieval_context': {
                        'query': 'fixture query', 'knowledge_base_id': 'same-kb',
                        'creation_settings': {'embedding_model_uuid': 'fixture-embedding'},
                        'retrieval_settings': {'top_k': 2}}})
                    assert result['total_found'] == 1, result
                    assert 'private-'+label in str(result), result
                for label in 'AB':
                    page = await call(label, R.PAGE_API, {'page_id': 'observability', 'endpoint': '/snapshot',
                        'method': 'GET', 'body': {}})
                    assert page['data']['counters']['ingest.total'] == 1, page
            else:
                for label in 'AB':
                    page = await call(label, R.PAGE_API, {'page_id': 'memory_console', 'endpoint': '/summary',
                        'method': 'GET', 'body': {}})
                    assert page.get('data') and page['data']['plugin_config']['max_profile_traits'] == (1 if label == 'A' else 4), page
            print(json.dumps({'plugin': CODE.name, 'sdk': '0.7.4', 'worker_pid': proc.pid,
                'same_worker_two_workspace_slots': True, 'slot_configs_distinct': True,
                'successful_representative_invocation': True,
                'host_actions': actions, 'scope': 'real SDK child RPC, local Host fixture; no production/upstream/sandbox proof'}))
        finally:
            proc.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 5)
            except asyncio.TimeoutError:
                proc.kill(); stdout, stderr = await proc.communicate()
            if proc.returncode not in (0, -15):
                print('child stderr:', stderr.decode()[-3500:], file=sys.stderr)
            assert b'SAME_OBJECT_GRAPH=true' in stderr, stderr.decode()[-3500:]
            assert proc.returncode in (0, -15), proc.returncode

if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[2] == '--child':
        asyncio.run(child(sys.argv[3]))
    else:
        asyncio.run(asyncio.wait_for(parent(), 60))
