# Runner stateless-v1 candidate audit (offline, no publication)

Source baseline: `eded61bdcbbf7775ffe043cb43b7b93281570152`. These are **source candidates**, not certifications. The signed schema-v2 review/digest claim must be issued separately against the exact built artifact before any shared placement. A legacy v1 certificate cannot gain this claim by a manifest edit.

| Legacy identity (`langbot-team/…`) | Candidate | Reason / follow-up |
| --- | --- | --- |
| LocalAgent | `componentModel: stateless-v1`, `0.1.11` | Process-scoped initialization is empty; component stores no tenant state. Per-run loop, tool workers, deadlines and API contexts are local and joined on cancellation. Existing real-SDK loopback shared-context tests run with explicit slot attach. Standalone project metadata/lock updated. |
| DeerFlowAgent | `componentModel: stateless-v1`, `0.1.11` | Empty initialization; stream deduplication state and client credentials are allocated per invocation. External thread id is passed via Runner context/state, not saved on the component. |
| WeKnoraAgent | `componentModel: stateless-v1`, `0.1.11` | Empty initialization; client and upstream session are invocation-scoped. Corrected upstream user namespace to use the active invocation binding, with concurrent-binding regression. |
| CozeAgent | **dedicated** | Asset gateway creates a persistent listener, stores per-run `Registration` with API/context, and creates detached server tasks; current component-owned pool is incompatible with stateless singleton. Identity binding corrected in preparation but **not** a shared eligibility claim. |
| DashScopeAgent | **dedicated** | Same asset gateway; additionally vendor subprocess spawn/reap uses detached tasks. Requires explicit SDK-owned revocable gateway/child process authority and bounded shutdown before claim. |
| DifyAgent | **dedicated** | Asset gateway as above; `_active_resumes` is a mutable shared component set guarding continuation replay. Moving it to per-invocation scope would break same-installation concurrent resume protection; requires Host-atomic continuation consumption and gateway redesign. |
| LangflowAgent | **dedicated** | Asset gateway holds per-tenant capabilities past the invocation in a listener task. Requires gateway redesign. |
| N8nAgent | **dedicated** | Same optional asset gateway; identity fix is preparatory only. Even if optional, the manifest claim covers *all* configurations. |
| TboxAgent | **dedicated** | Vendor subprocess lifecycle creates detached spawn/reap tasks; no stateless claim until runtime-owned process/cancellation authority is proven. Identity fix is preparatory only. |

Bounded verification: `PYTHONPATH=/home/rock/work/langbot-plugin-sdk-stateless/src /home/rock/work/langbot-plugin-sdk-stateless/.venv/bin/python -m pytest -q Runner/LocalAgent/tests -k 'not real_sdk_dependency_admission' Runner/tests/test_stateless_v1_candidate.py` (real candidate SDK source, simulated Host; no live provider). Dependency installation smoke intentionally excluded; no live signed-schema-v2 admission, real subprocess multi-slot proof for all three plugins, or production provider acceptance is claimed. The 0.7.3 SDK checkout and offline lock resolution (0.7.4) need release compatibility confirmation before publication.
