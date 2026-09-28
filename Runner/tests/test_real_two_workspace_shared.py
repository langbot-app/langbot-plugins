"""Offline two-Workspace proof against unmodified Git-built Runner archives.

Run separately with the stateless SDK checkout on PYTHONPATH; no credentials,
production tenants, marketplace signing, or external provider calls are used.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml
from langbot_plugin.entities.io.actions.enums import (
    PluginToRuntimeAction,
    RuntimeToPluginAction,
)
from langbot_plugin.entities.io.context import InstallationBinding

ROOT = Path(__file__).resolve().parents[2]
SDK = Path('/home/rock/work/langbot-sdk-stateless-shared')
FOLDERS = ('RunnerDemo', 'LocalAgent', 'deerflow-agent', 'weknora-agent')


def package(folder: str, dest: Path) -> tuple[Path, str]:
    """Build once from HEAD tracked bytes, never rebuild between bindings."""
    source = dest / 'source'
    source.mkdir()
    archive = subprocess.check_output(['git', 'archive', 'HEAD', f'Runner/{folder}'], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest / 'git', filter='data')
    original = dest / 'git' / 'Runner' / folder
    for entry in original.iterdir():
        if entry.name in {'tests', 'scripts', '.github', 'dist'}:
            continue
        target = source / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)
    env = os.environ.copy()
    env['PYTHONPATH'] = str(SDK / 'src')
    subprocess.run([sys.executable, '-m', 'langbot_plugin.cli.__init__', 'build', '-o', str(dest / 'built')], cwd=source, env=env, check=True, stdout=subprocess.DEVNULL)
    packages = list((dest / 'built').glob('*.lbpkg'))
    assert len(packages) == 1
    data = packages[0].read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    installed = dest / 'installed'
    installed.mkdir()
    with zipfile.ZipFile(io.BytesIO(data)) as zipf:
        assert zipf.comment == b''  # unsigned candidate, not Cloud shared admission
        assert zipf.testzip() is None
        zipf.extractall(installed)
    assert yaml.safe_load((installed / 'manifest.yaml').read_text())['execution']['componentModel'] == 'stateless-v1'
    return installed, digest


def binding(digest: str, name: str, rev: int = 1) -> InstallationBinding:
    return InstallationBinding(instance_uuid='local-instance', workspace_uuid=f'workspace-{name}', placement_generation=1,
                               installation_uuid=f'installation-{name}', runtime_revision=rev, artifact_digest=digest)


class Worker:
    def __init__(self, proc):
        self.proc = proc
        self.seq = 100
        self.callbacks = []

    async def send(self, action, data, context=None):
        self.seq += 1
        seq = self.seq
        payload = {'seq_id': seq, 'action': action.value, 'data': data}
        if context is not None:
            payload['context'] = context.model_dump()
        self.proc.stdin.write((json.dumps(payload) + '\n').encode())
        await self.proc.stdin.drain()
        responses = []
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 8)
            if not line:
                stderr = await self.proc.stderr.read()
                raise AssertionError(f'worker exited: {stderr.decode(errors="replace")[-3000:]}')
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get('action'):
                self.callbacks.append(message)
                assert message.get('context') is not None, message
                callback_binding = InstallationBinding.model_validate(message['context'])
                assert callback_binding == context, (message, context)
                assert message['action'] == PluginToRuntimeAction.CALL_TOOL.value, message
                self.proc.stdin.write((json.dumps({'seq_id': message['seq_id'], 'code': 0,
                    'message': 'offline fixture', 'data': {'result': {'mock': True,
                        'workspace': callback_binding.workspace_uuid}}}) + '\n').encode())
                await self.proc.stdin.drain()
            elif message.get('seq_id') == seq:
                responses.append(message)
                if message.get('chunk_status') != 'continue' or (action != RuntimeToPluginAction.RUN_RUNNER and len(responses) == 1):
                    return responses


async def launch(path: Path, dest: Path) -> Worker:
    # Instrument *SDK controller only*, not the immutable candidate archive.
    # This emits actual object IDs after each SDK slot attach.
    hook = dest / 'hook'
    hook.mkdir(exist_ok=True)
    (hook / 'sitecustomize.py').write_text('''import json, os
from langbot_plugin.cli.run.controller import PluginRuntimeController
original = PluginRuntimeController.initialize_slot
async def observed(self, binding, settings):
    slot = await original(self, binding, settings)
    graph = self.plugin_container
    with open(os.environ['PROOF_GRAPH_LOG'], 'a') as log:
        log.write(json.dumps({'binding': binding.model_dump(), 'pid': os.getpid(),
            'plugin': id(graph.plugin_instance),
            'components': {c.manifest.metadata.name: id(c.component_instance) for c in graph.components}}) + '\\n')
    return slot
PluginRuntimeController.initialize_slot = observed
''')
    env = os.environ.copy()
    env.update(PYTHONPATH=f'{hook}:{SDK / "src"}', LANGBOT_PLUGIN_REGISTRATION_CAPABILITY='x' * 40,
               LANGBOT_PLUGIN_RUNTIME_PROFILE='shared', LANGBOT_PLUGIN_FILE_STORAGE_DIR=str(dest / 'transfer'),
               PROOF_GRAPH_LOG=str(dest / 'graph.jsonl'), PYTHONUNBUFFERED='1')
    proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'langbot_plugin.cli.__init__', 'run', '-s', '--prod',
        cwd=path, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    for _ in range(20):
        line = await asyncio.wait_for(proc.stdout.readline(), 8)
        if not line:
            raise AssertionError((await proc.stderr.read()).decode()[-3000:])
        try:
            registration = json.loads(line)
        except json.JSONDecodeError:
            continue
        if registration.get('action') == PluginToRuntimeAction.REGISTER_PLUGIN.value:
            proc.stdin.write((json.dumps({'seq_id': registration['seq_id'], 'code': 0, 'message': 'ok', 'data': {}}) + '\n').encode())
            await proc.stdin.drain()
            return Worker(proc)
    raise AssertionError('No registration')


@pytest.mark.asyncio
@pytest.mark.parametrize('folder', FOLDERS)
async def test_two_workspace_same_archive_shared_worker(folder, tmp_path):
    installed, digest = package(folder, tmp_path)
    a, b = binding(digest, 'A'), binding(digest, 'B')
    worker = await launch(installed, tmp_path)
    try:
        config_a = {'tenant_marker': 'A', 'nested': {'values': [1, {'owner': 'A'}]}}
        config_b = {'tenant_marker': 'B', 'nested': {'values': [2, {'owner': 'B'}]}}
        def settings(config):
            return {'plugin_settings': {'enabled': True, 'priority': 0, 'plugin_config': config}}
        for bound, config in ((a, config_a), (b, config_b)):
            result = await worker.send(RuntimeToPluginAction.ATTACH_PLUGIN_SLOT, settings(config), bound)
            assert result[-1]['code'] == 0, result
            slot = await worker.send(RuntimeToPluginAction.GET_PLUGIN_SLOT_CONTAINER, {}, bound)
            assert slot[-1]['code'] == 0 and slot[-1]['data']['plugin_config'] == config, slot
        graph = [json.loads(line) for line in (tmp_path / 'graph.jsonl').read_text().splitlines()]
        assert len(graph) == 2 and {row['pid'] for row in graph} == {worker.proc.pid}
        assert graph[0]['plugin'] == graph[1]['plugin']
        assert graph[0]['components'] == graph[1]['components']
        declared = [p.stem for p in (installed / 'components' / 'runner').glob('*.yaml')]
        assert set(graph[0]['components']) == set(declared)
        assert worker.proc.returncode is None
        # RunnerDemo executes real observer and outbound-authorized community
        # handlers. Others are no-credential object-lifecycle probes only.
        runner_name = 'observer' if folder == 'RunnerDemo' else 'default'
        ctx = {'run_id': 'offline-A', 'trigger': {'type': 'message.received'},
            'event': {'event_id': 'offline-A', 'event_type': 'message.received', 'source': 'test',
                      'data': {'type': 'message.received', 'adapter_name': 'test', 'bot_uuid': 'bot'}},
            'input': {}, 'delivery': {'surface': 'test'}, 'resources': {}, 'runtime': {},
            'config': {'language': 'en_US'}, 'variables': {}}
        if folder == 'RunnerDemo':
            for bound, owner in ((a, 'A'), (b, 'B')):
                ctx['run_id'] = f'offline-{owner}'
                ctx['event']['event_id'] = f'offline-{owner}'
                result = await worker.send(RuntimeToPluginAction.RUN_RUNNER,
                    {'runner_name': runner_name, 'context': ctx}, bound)
                assert all(row['code'] == 0 for row in result), result
                assert any(row.get('data', {}).get('type') == 'run.completed' for row in result), result
                event = json.loads((ROOT / 'Runner' / folder / 'examples' / '01-member-joined.json').read_text())
                ctx['event']['event_type'] = event['event_type']
                ctx['event']['data'] = {'type': event['event_type'], **event['data']}
                ctx['trigger']['type'] = event['event_type']
                ctx['config'] = {'language': 'en_US', 'welcome_text': owner}
                ctx['resources'] = {'tools': [
                    {'tool_name': name, 'operations': ['call']}
                    for name in ('event_get_actor', 'event_get_group', 'event_reply')
                ]}
                before = len(worker.callbacks)
                result = await worker.send(RuntimeToPluginAction.RUN_RUNNER,
                    {'runner_name': 'community', 'context': ctx}, bound)
                assert any(row.get('data', {}).get('type') == 'run.completed' for row in result), result
                assert [row['data']['tool_name'] for row in worker.callbacks[before:]] == [
                    'event_get_actor', 'event_get_group', 'event_reply']
                assert owner in str(result)
                ctx['event']['event_type'] = 'message.received'
                ctx['event']['data'] = {'type': 'message.received', 'adapter_name': 'test', 'bot_uuid': 'bot'}
                ctx['trigger']['type'] = 'message.received'
        else:
            # Empty runner config fails before any external provider call; this
            # still exercises real SDK dispatch on each shared component.
            for bound, owner in ((a, 'A'), (b, 'B')):
                ctx['run_id'] = f'offline-{owner}'
                ctx['event']['event_id'] = f'offline-{owner}'
                result = await worker.send(RuntimeToPluginAction.RUN_RUNNER,
                    {'runner_name': runner_name, 'context': ctx}, bound)
                assert any(row.get('data', {}).get('type') == 'run.failed' for row in result), result
            assert worker.callbacks == []
        revised_a = binding(digest, 'A', rev=2)
        revised_config = {'tenant_marker': 'A2', 'nested': {'values': [3, {'owner': 'A2'}]}}
        updated = await worker.send(RuntimeToPluginAction.ATTACH_PLUGIN_SLOT, settings(revised_config), revised_a)
        assert updated[-1]['code'] == 0, updated
        graph2 = [json.loads(line) for line in (tmp_path / 'graph.jsonl').read_text().splitlines()]
        assert graph2[-1]['plugin'] == graph[0]['plugin'] and graph2[-1]['components'] == graph[0]['components']
        stale = await worker.send(RuntimeToPluginAction.DETACH_PLUGIN_SLOT, {}, a)
        assert stale[-1]['code'] != 0, stale
        assert (await worker.send(RuntimeToPluginAction.GET_PLUGIN_SLOT_CONTAINER, {}, revised_a))[-1]['data']['plugin_config'] == revised_config
        assert (await worker.send(RuntimeToPluginAction.GET_PLUGIN_SLOT_CONTAINER, {}, b))[-1]['data']['plugin_config'] == config_b
        assert (await worker.send(RuntimeToPluginAction.DETACH_PLUGIN_SLOT, {}, revised_a))[-1]['code'] == 0
        assert (await worker.send(RuntimeToPluginAction.GET_PLUGIN_SLOT_CONTAINER, {}, b))[-1]['data']['plugin_config'] == config_b
        assert worker.proc.returncode is None
        assert (await worker.send(RuntimeToPluginAction.DETACH_PLUGIN_SLOT, {}, b))[-1]['code'] == 0
        print(f'PROOF {folder} sha256={digest} pid={worker.proc.pid} graph={graph[0]["plugin"]}/{graph[0]["components"]} callbacks={len(worker.callbacks)}')
    finally:
        if worker.proc.returncode is None:
            worker.proc.terminate()
            await asyncio.wait_for(worker.proc.wait(), 8)
