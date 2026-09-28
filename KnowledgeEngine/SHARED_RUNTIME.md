# KnowledgeEngine shared-worker readiness (local source candidate)

**Decision: 0/5 ready for a signed `stateless-v1` / `shared-runtime-v1` release.**
The five manifests remain dedicated: no certificate, marketplace publication, or
Cloud shared-worker execution is claimed. The previous SDK 0.6.1 fixture used
separate plugin/component graphs and is not one-worker/two-Workspace evidence.
Do not restore `execution.sharedRuntime` merely because state is labeled by KB or
because the SDK provides task-local Host proxies. The SDK 0.7.4 contract requires
one actual BasePlugin and one instance of each declared component per digest;
`initialize()` runs once without tenant configuration.

| Plugin | Integrated source correction | Remaining blocker / release gate |
| --- | --- | --- |
| DifyDatasetsConnector | Current main returns the real Dify upload document ID, fails ingestion if absent, and persists KB config through the SDK Host proxy. | `ConfigStore._state` is one component-wide lock/fence and `SerialState.run` dispatches tenant-bearing `create_task()` across cancellation. Host must persist the exact Host-file → upstream-document mapping and delete with that ID, requiring real upstream absence verification; a Host file UUID is not a substitute. Shared placement blocked. |
| FastGPTConnector | Current main returns the real collection ID, rejects missing ID, reads `data.list` with typed scores and uses `DELETE .../collection/delete?id=...`. | Same singleton/global mutation fence, detached task, and Host-file → collection-ID lifecycle blocker. Shared placement blocked. |
| RAGFlowConnector | Upload now treats an acknowledged empty document list as **ambiguous remote outcome requiring reconciliation**, not a failed upload that is safe to retry; nonempty real document IDs continue to be returned. | Same component-wide fence and detached task. Parsing can fail after upload, so deletion/reconciliation must use the returned upstream ID and actual dataset, not Host UUID. Shared placement blocked. |
| LangRAG | Candidate moves telemetry history/Page reads to invocation-bound Host storage, removes initialize-time tenant reads and shared telemetry store, scopes serialization/fence by installation binding, and runs parser threads with an empty context while waiting for settlement. | Still no real one-worker A/B subprocess proof; CPU threads carry document content until completion and mutation uncertainty/cancellation must be reconciled with Host. Telemetry errors cannot be called a durable accounting ledger. Shared placement blocked. |
| LongTermMemory | Candidate removes KB/profile caches from the singleton, resolves profile limits through invocation config instead of `initialize()`, and serializes/fences selected storage RMW paths per binding. | Audit every Host vector/storage path and nested episode operation for ambiguous partial commits, cancellation and config revision; establish same-object A/B proof across Page/Tool/Command/Engine/EventListener with distinct limits and storage. Shared placement blocked. |

The three connector fixes above are **not** proof of real provider lifecycle. A
successful-looking upload with no upstream ID must not be replaced with a Host
file UUID; transport failure after remote commit can create an orphan. The
binding-local storage key is an SDK proxy, not an independently authenticated
provider account. If two installations deliberately configure one provider
account/dataset, the connector cannot manufacture provider-side isolation.

Before opting in, replace connector tenant-bearing detached operations with an
SDK-owned revocable/settled invocation lifecycle or a bounded in-invocation
operation with fail-closed reconciliation. Scope mutation ordering and fencing
by complete installation binding; verify that same-binding config writes and
KB tombstones cannot reorder. Add durable, Workspace/KB/file-scoped upstream ID
mapping and make Host deletion retain its row until actual upstream success.
Reconcile ambiguous retries rather than creating a second remote document.
Then run an actual released-SDK worker with two Workspace bindings on the same
PID, BasePlugin, and component identities, plus representative successful A/B
invocations, storage and config-revision isolation, cancellation/revocation,
and provider document absence after deletion. These are release gates, not
claims established by this source-only change. No new unit tests were added.
