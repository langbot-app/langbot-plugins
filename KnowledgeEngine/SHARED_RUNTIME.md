# KnowledgeEngine shared-runtime source gate

**Only LangRAG is an opt-in local source candidate.** No signature, marketplace publication, Cloud admission, sandbox, real-provider execution, or production readiness is claimed. LongTermMemory and all three external connectors remain dedicated.

## LangRAG evidence and limits

The SDK 0.7.4 subprocess probe `certification-tests/shared_subprocess_probe.py` launches a separate Python worker, creates exactly one `PluginRuntimeController` / `BasePlugin` / each declared component object, attaches two distinct `InstallationBinding` Workspaces on the same WebSocket transport and asserts the object identities coincide inside that child. Both Workspaces successfully ingest and retrieve distinct markers with the same Host IDs; the Page snapshot succeeds for both. The Host vector/storage/embedding APIs are local scoped fixtures, not production services. Reproduce from repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 /home/rock/work/ke-shared-test-venv/bin/python KnowledgeEngine/certification-tests/shared_subprocess_probe.py KnowledgeEngine/LangRAG
```

The single run does not establish revocation races, production persistence, config revision, or deterministic vector repair following ambiguous partial commits. The previous source correction preserves binding-local fences, invocation-bound telemetry, and settled CPU offload; telemetry is diagnostic only, not an accounting ledger. Keep release/certification gated on those separate checks.

## LongTermMemory

The same real-child probe temporarily overrides the in-memory manifest to shared mode (without changing the dedicated artifact), attaches two Workspace slots and invokes both Page `/summary` endpoints with distinct task-local profile limits, proving the SDK object graph can host both slots. It does **not** exercise representative writes through Tool, Command, KnowledgeEngine and EventListener, nor interrupted nested episode operations, so its manifest remains dedicated. Run the probe with `KnowledgeEngine/LongTermMemory` to reproduce the limited evidence.

## External connectors: still blocked

Dify, FastGPT, and RAGFlow now serialize mutations and fence ambiguous errors per knowledge base within the complete installation binding, without creating a tenant-bearing detached task; one ambiguous knowledge base never blocks its siblings sharing the installation. A durable plugin-storage intent precedes upload; Host file ID, KB ID, upstream ID, dataset, and state are recorded under an installation-bound key when returned. Deletion refuses missing/pending mappings and mismatched datasets, resolves either the Host ID or an exact previously recorded upstream ID, and records a tombstone after upstream acknowledgement. Only a dispatched mutation whose outcome is unknown fences: a deterministic pre-call validation error and ordinary pre-dispatch cancellation do not. Fences live in Host storage under a knowledge-base-scoped key with an in-process cache, so a restarted worker still refuses that knowledge base, and deleting the knowledge base clears its fence; the plugins also implement `BasePlugin.on_installation_revoked` to drop per-binding process-local locks and fence cache on SDK revisions that dispatch it (never called by langbot-plugin 0.7.4). **Do not enable shared placement or claim integrated deletion.**

The Host may already persist `IngestionResult.document_id` and pass that upstream ID on delete, but this source alone cannot prove which deployed Host revision is used. Provider DELETE acknowledgements here are not yet verified with an authoritative upstream list/readback, and an upload timeout after remote commit leaves a durable pending intent requiring manual reconciliation. Config KB tombstones must not precede cleanup of outstanding mappings; the Host needs to retain the file row until verified upstream absence. For RAGFlow, an acknowledged upload followed by failed parse needs reconciliation rather than blind reupload. Existing connector fixture tests written for deleting arbitrary unrecorded IDs now fail by design; cancellation fixtures with deliberately detached Host commits no longer model the SDK-owned revocable action. Replacement fixture tests cover per-knowledge-base fencing and sibling isolation, fence persistence across a worker restart, fence clearing on knowledge-base deletion, ambiguous provider/transport failures, non-fencing of deterministic validation errors and pre-dispatch cancellation, and the revocation hook. Real upstream A/B ingestion, deletion absence and cancellation/revision proof remain release gates.

No published certification or shared claim is made for those four plugins.
