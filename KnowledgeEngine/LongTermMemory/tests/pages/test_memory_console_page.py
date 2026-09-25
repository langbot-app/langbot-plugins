from __future__ import annotations

import pytest

from components.pages.memory_console.memory_console import MemoryConsolePage
from langbot_plugin.api.definition.components.page import PageRequest


class FakeStore:
    EPISODE_STATUS_ACTIVE = "active"
    EPISODE_STATUS_SUPERSEDED = "superseded"
    EPISODE_STATUS_ARCHIVED = "archived"
    EPISODE_STATUS_DELETED = "deleted"
    EPISODE_STATUSES = {
        EPISODE_STATUS_ACTIVE,
        EPISODE_STATUS_SUPERSEDED,
        EPISODE_STATUS_ARCHIVED,
        EPISODE_STATUS_DELETED,
    }

    def __init__(self):
        self.audit_entries: list[dict] = []
        self.status_updates: list[dict] = []
        self.deleted: list[dict] = []
        self.list_calls: list[dict] = []
        self.probe_calls: list[dict] = []
        self.snapshots: dict[str, dict] = {}
        self.episodes = [
            {
                "id": "ep-1",
                "content": "Alice prefers concise answers",
                "tags": ["preference"],
                "importance": 3,
                "timestamp": "2026-01-01T00:00:00Z",
                "status": "active",
            },
            {
                "id": "ep-2",
                "content": "Alice corrected a preference",
                "tags": ["correction"],
                "importance": 2,
                "timestamp": "2026-01-02T00:00:00Z",
                "status": "superseded",
            },
        ]

    async def update_episode_status(
        self,
        collection_id,
        embedding_model_uuid,
        episode_id,
        user_key,
        status,
    ):
        self.status_updates.append({
            "collection_id": collection_id,
            "embedding_model_uuid": embedding_model_uuid,
            "episode_id": episode_id,
            "user_key": user_key,
            "status": status,
        })
        if episode_id == "missing":
            return None
        return {"id": episode_id, "status": status}

    async def delete_episode_by_id(self, collection_id, episode_id, user_key):
        self.deleted.append({
            "collection_id": collection_id,
            "episode_id": episode_id,
            "user_key": user_key,
        })
        return 1

    async def list_episodes(
        self,
        collection_id,
        user_key,
        limit=20,
        offset=0,
        include_statuses=None,
    ):
        self.list_calls.append({
            "collection_id": collection_id,
            "user_key": user_key,
            "limit": limit,
            "offset": offset,
            "include_statuses": include_statuses,
        })
        statuses = set(include_statuses or ["active"])
        episodes = [ep for ep in self.episodes if ep["status"] in statuses]
        return episodes[offset: offset + limit], len(episodes)

    async def append_audit_entry(self, **entry):
        self.audit_entries.append(entry)
        return entry

    async def list_audit_entries(self, scope_key, limit=10, offset=0):
        entries = list(reversed(self.audit_entries))
        return entries[offset: offset + limit], len(entries)

    async def export_audit_entries(self, scope_key):
        return list(self.audit_entries)

    async def export_profiles_by_scope(self, scope_key):
        return [{
            "type": "session",
            "scope_key": scope_key,
            "profile": {"name": "Release group"},
        }]

    async def get_kb_configs(self):
        return {
            "kb-1": {"embedding_model_uuid": "emb-1", "isolation": "session"},
        }

    async def run_metadata_filter_probe(self, collection_id, embedding_model_uuid):
        self.probe_calls.append({
            "collection_id": collection_id,
            "embedding_model_uuid": embedding_model_uuid,
        })
        return {
            "status": "OK",
            "checks": [
                {"id": "write", "status": "OK", "detail": "wrote probe records"},
                {"id": "search", "status": "OK", "detail": "search respects filter"},
            ],
        }

    async def get_injection_snapshot(self, scope_key):
        return self.snapshots.get(scope_key)


class FakePlugin:
    def __init__(self):
        self.memory_store = FakeStore()


async def _post(page: MemoryConsolePage, endpoint: str, body: dict):
    response = await page.handle_api(
        PageRequest(endpoint=endpoint, method="POST", body=body)
    )
    assert response.error is None
    return response.data


@pytest.fixture
def page() -> MemoryConsolePage:
    component = MemoryConsolePage()
    component.plugin = FakePlugin()
    return component


@pytest.mark.asyncio
async def test_episode_status_update_writes_scoped_audit(page):
    data = await _post(page, "/episodes/status", {
        "collection_id": "kb-1",
        "embedding_model_uuid": "emb-1",
        "scope_key": "bot-1:group_1",
        "user_key": "bot-1:group_1",
        "episode_id": "ep-1",
        "status": "archived",
    })

    assert data["episode"] == {"id": "ep-1", "status": "archived"}
    assert page.plugin.memory_store.status_updates == [{
        "collection_id": "kb-1",
        "embedding_model_uuid": "emb-1",
        "episode_id": "ep-1",
        "user_key": "bot-1:group_1",
        "status": "archived",
    }]
    assert page.plugin.memory_store.audit_entries[-1]["operation"] == "archived"
    assert page.plugin.memory_store.audit_entries[-1]["scope_key"] == "bot-1:group_1"
    assert page.plugin.memory_store.audit_entries[-1]["user_key"] == "bot-1:group_1"


@pytest.mark.asyncio
async def test_episode_delete_writes_scoped_audit(page):
    data = await _post(page, "/episodes/delete", {
        "collection_id": "kb-1",
        "scope_key": "bot-1:group_1",
        "user_key": "bot-1:group_1",
        "episode_id": "ep-1",
    })

    assert data == {"episode_id": "ep-1", "deleted": 1}
    assert page.plugin.memory_store.deleted == [{
        "collection_id": "kb-1",
        "episode_id": "ep-1",
        "user_key": "bot-1:group_1",
    }]
    assert page.plugin.memory_store.audit_entries[-1]["operation"] == "delete"
    assert page.plugin.memory_store.audit_entries[-1]["metadata"]["deleted"] == 1


@pytest.mark.asyncio
async def test_episode_export_uses_current_user_scope_and_statuses(page):
    data = await _post(page, "/episodes/export", {
        "collection_id": "kb-1",
        "scope_key": "bot-1:group_1",
        "user_key": "bot-1:group_1",
        "statuses": ["active", "superseded"],
    })

    assert data["scope_key"] == "bot-1:group_1"
    assert data["user_key"] == "bot-1:group_1"
    assert data["count"] == 2
    assert {episode["id"] for episode in data["episodes"]} == {"ep-1", "ep-2"}
    assert page.plugin.memory_store.list_calls[0]["include_statuses"] == [
        "active",
        "superseded",
    ]
    assert page.plugin.memory_store.audit_entries[-1]["operation"] == "export_l2"


@pytest.mark.asyncio
async def test_profile_export_writes_scoped_audit(page):
    data = await _post(page, "/export-profiles", {
        "scope_key": "bot-1:group_1",
        "user_key": "bot-1:group_1",
    })

    assert data["scope_key"] == "bot-1:group_1"
    assert data["user_key"] == "bot-1:group_1"
    assert data["count"] == 1
    assert data["profiles"][0]["profile"]["name"] == "Release group"
    assert page.plugin.memory_store.audit_entries[-1]["operation"] == "export_profiles"
    assert page.plugin.memory_store.audit_entries[-1]["target_type"] == "profile"


@pytest.mark.asyncio
async def test_health_runs_probe_and_combines_status(page):
    data = await _post(page, "/health", {
        "collection_id": "kb-1",
        "embedding_model_uuid": "emb-1",
    })

    assert data["status"] == "OK"
    check_ids = [check["id"] for check in data["checks"]]
    assert check_ids[:2] == ["kb_config", "embedding"]
    assert "search" in check_ids
    assert page.plugin.memory_store.probe_calls == [{
        "collection_id": "kb-1",
        "embedding_model_uuid": "emb-1",
    }]


@pytest.mark.asyncio
async def test_health_skips_probe_when_kb_missing(page):
    data = await _post(page, "/health", {
        "collection_id": "missing-kb",
        "embedding_model_uuid": "emb-1",
    })

    assert data["status"] == "ERROR"
    statuses = {check["id"]: check["status"] for check in data["checks"]}
    assert statuses["kb_config"] == "ERROR"
    assert statuses["probe"] == "WARN"
    assert page.plugin.memory_store.probe_calls == []


@pytest.mark.asyncio
async def test_health_falls_back_to_configured_embedding_model(page):
    data = await _post(page, "/health", {"collection_id": "kb-1"})

    assert data["status"] == "OK"
    assert page.plugin.memory_store.probe_calls == [{
        "collection_id": "kb-1",
        "embedding_model_uuid": "emb-1",
    }]


@pytest.mark.asyncio
async def test_injection_returns_snapshot_when_present(page):
    page.plugin.memory_store.snapshots["bot-1:group_1"] = {
        "injected": True,
        "block_count": 2,
        "episodes": [{"content": "Alice flies to Tokyo next Tuesday"}],
    }

    data = await _post(page, "/injection", {"scope_key": "bot-1:group_1"})

    assert data["scope_key"] == "bot-1:group_1"
    assert data["snapshot"]["injected"] is True
    assert data["snapshot"]["episodes"][0]["content"].startswith("Alice")


@pytest.mark.asyncio
async def test_injection_returns_none_when_absent(page):
    data = await _post(page, "/injection", {"scope_key": "bot-1:group_9"})

    assert data == {"scope_key": "bot-1:group_9", "snapshot": None}
