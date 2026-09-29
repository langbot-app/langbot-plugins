# 4.11 official Runner + KnowledgeEngine source ledger

Integration base: `origin/main` at `9be9e9291ebc848c6fdd8006c50e6226bd822649` (fetched before integration). Each `source tree` is the Git tree object for the indicated plugin directory at commit `bb34232fd` (the shared-placement commit), unless the row says otherwise; resolve it at review time; it identifies committed source, not a built ZIP, normalized artifact digest, signature, or public marketplace version. Versions below are the integration manifest versions, **not verified live versions**. Author namespace is `langbot-team` for all 18 IDs.

| Official ID | Manifest version | Source tree | Placement claim | Source lane |
| --- | --- | --- | --- | --- |
| LocalAgent | 0.2.1 | `3c30db89ad68360b1e21f47f4ceee672fe6836be` | shared candidate | four Runner `8bb55ec` |
| RunnerDemo | 0.2.0 | `0b8179f6c5d054bca080cd333602fb05972abd78` | shared candidate | four Runner `8bb55ec` |
| ACPAgentRunner | 0.1.16 | `a5fcac59610a67957cc7c2b136eb55bb3dfb6d43` | dedicated only | nine Runner `ede6756` |
| ClaudeCodeAgent | 0.1.14 | `d7e7442551d333166fc6fd6c2b863b3a1d4d89bb` | dedicated only | nine Runner `ede6756` |
| CodexAgent | 0.1.20 | `897281bc02835e73b1877acfb34aed51fd561763` | dedicated only | nine Runner `ede6756` |
| CozeAgent | 0.2.0 | `0a7168acf038bc6c4331b6315c4ba261a682604a` | shared candidate | nine Runner `ede6756` |
| DashScopeAgent | 0.2.0 | `8ddf459a754461b8b5b0f253e3df603d600fa840` | shared candidate | nine Runner `ede6756` |
| DeerFlowAgent | 0.2.0 | `da1275c8693a177ecb7cebb7fc91c3be1b4012ea` | shared candidate | four Runner `8bb55ec` |
| DifyAgent | 0.2.0 | `a45e3ebe79d31a9ddf4daccaae6fcfbf72f96a91` | shared candidate | nine Runner `ede6756` |
| LangflowAgent | 0.2.0 | `e351b9f9a8a8550a0631d7d6e895a975f947f07d` | shared candidate | nine Runner `ede6756` |
| N8nAgent | 0.2.0 | `fb0cede58b9ff07f65c0270665befde82be70767` | shared candidate | nine Runner `ede6756` |
| TboxAgent | 0.2.0 | `c8dfbcf4f2dd1f094b5ef712b09db771aa5f3dd0` | shared candidate | nine Runner `ede6756` |
| WeKnoraAgent | 0.2.0 | `b86401e05e285e9fd1080ea2395176b94ade2c2d` | shared candidate | four Runner `8bb55ec` |
| DifyDatasetsConnector | 0.2.0 | `fb516e590cd025d109d68980c0bdc6b337bb5931` | shared candidate | KE `ee265c0` |
| FastGPTConnector | 0.2.0 | `79112c601820cedd0004c114ef304c51faa1b9c6` | shared candidate | KE `ee265c0` |
| LangRAG | 0.2.1 | `6ff50a5a4455e44fbc816ebed6596b8427cbd7b8` | shared candidate | KE `ee265c0` |
| LongTermMemory | 0.2.0 | `7cb613ebb9632b7945e51274ba31d78cfdd51340` | shared candidate | KE `ee265c0` |
| RAGFlowConnector | 0.2.0 | `0af91f67a482a3a7f0aab487d5e12ffa0245c7a9` | shared candidate | KE `ee265c0` |

Two rows were re-integrated after `bb34232fd` and are recorded at `9c68078`: **LangRAG** and **LocalAgent**, both 0.2.1. LangRAG fences a cancelled caller's dispatched vector mutation and a reply whose count is not a nonnegative integer again, and telemetry failures no longer fail the operation they describe; LocalAgent's tree differs only in removed tests and its lockfile version. `misc/GeneralParsers` sits outside this ledger: its trimmed test tree moved it to 0.1.9.

Fifteen source candidates declare both `shared-runtime-v1` and `stateless-v1`; three stay dedicated (the three native coding Runners, whose process-local daemon hubs, shared workspace root and worker-HOME credentials cannot serve two installations from one process). The four KnowledgeEngine plugins moved from dedicated to shared candidate: their locks and fences are keyed by (installation binding, knowledge-base identity), persisted in installation-bound Host storage, released on installation revocation, and only a dispatched mutation with an unknown outcome fences. Ambiguous upstream mutations still need manual reconciliation, but that requirement is identical under dedicated and shared placement and is not a placement blocker. None is asserted certified or production-shared. The source tree identities include integration-only SDK requirement corrections: the four Runner candidates from `8bb55ec` now require `langbot-plugin>=0.7.4,<0.8`, and LangRAG now requires the same instead of `==0.6.1`; their source trees therefore intentionally differ from the lane tips. CI and contract corrections later changed some source trees; the per-plugin tree column identifies the current PR head, not the earlier lane tips.

**Evidence classes:** The nine-Runner `probe_two_bindings.py` uses a real SDK Worker and synthetic Host/vendor fixture; its synthetic artifact digest is not a built archive hash. The KE `shared_subprocess_probe.py` and checked-in JSON evidence use a real SDK child and local Host vector/storage/embedding fixtures; LongTermMemory is now a shared candidate, so its former in-probe manifest override is no longer the placement difference. These demonstrate only the operations actually invoked, not real vendor/provider execution. RunnerDemo additionally has exact unsigned archive `f790d29da6d7d8461df35ffc7594eb73cd5d0e100e61e86264ffe843587703ba` from its *earlier lane* and real SDK 0.7.4 Worker dual-binding proof with controlled Host fixtures; it is not the archive of this integrated PR head. The four-Runner lane does not provide real provider acceptance. Real provider-account invocation, signed/downloaded ZIP identity, independent review, production sandbox/network policy, and two real Cloud Workspace installations sharing the same certified digest remain unproven. External KE connector ingest/delete/cancellation and LongTermMemory representative write paths are still release gates; do not interpret a successful local fixture or a marketplace badge as production proof.

No archive was built or published from this integration head. Before any release, build each candidate from the exact committed source and record its immutable archive digest, dependency preparation, certificate fields/signature, downloaded public artifact readback, provider invocation, and two-binding Cloud object/PID/authority evidence separately. No public or production writes were performed by this integration.
