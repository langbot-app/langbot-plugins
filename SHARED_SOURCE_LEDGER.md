# 4.11 official Runner + KnowledgeEngine source ledger

Integration base: `origin/main` at `9be9e9291ebc848c6fdd8006c50e6226bd822649` (fetched before integration). Each `source tree` is the Git tree object for the indicated plugin directory in the **current PR head** `180587a4ef2267c56f11596a4baae55e7253ab68`; it identifies committed source, not a built ZIP, normalized artifact digest, signature, or public marketplace version. Versions below are the integration manifest versions, **not verified live versions**. Author namespace is `langbot-team` for all 18 IDs.

| Official ID | Manifest version | Source tree | Placement claim | Source lane |
| --- | --- | --- | --- | --- |
| LocalAgent | 0.1.11 | `c683c6a65a79f64bdaa2227beff9c9d891899458` | shared candidate | four Runner `8bb55ec` |
| RunnerDemo | 0.1.7 | `e782b3b91c8d3aeb4ebc3b560c41081792b1597b` | shared candidate | four Runner `8bb55ec` |
| ACPAgentRunner | 0.1.16 | `a5fcac59610a67957cc7c2b136eb55bb3dfb6d43` | dedicated only | nine Runner `ede6756` |
| ClaudeCodeAgent | 0.1.14 | `d7e7442551d333166fc6fd6c2b863b3a1d4d89bb` | dedicated only | nine Runner `ede6756` |
| CodexAgent | 0.1.20 | `897281bc02835e73b1877acfb34aed51fd561763` | dedicated only | nine Runner `ede6756` |
| CozeAgent | 0.1.12 | `6ed88610fd27197b824e250a8baef5272d2d782f` | shared candidate | nine Runner `ede6756` |
| DashScopeAgent | 0.1.12 | `a869b73bb5d61758592a02e18502f0a1cb111199` | shared candidate | nine Runner `ede6756` |
| DeerFlowAgent | 0.1.11 | `89985ea893b85fb998aaaed47984effbc668fa27` | shared candidate | four Runner `8bb55ec` |
| DifyAgent | 0.1.12 | `a450a6f1362cb20a634dd3a70784d198b82ade99` | shared candidate | nine Runner `ede6756` |
| LangflowAgent | 0.1.12 | `6593c860899ba3b50ca97373509b6cb7814fbc06` | shared candidate | nine Runner `ede6756` |
| N8nAgent | 0.1.12 | `64fbe5a727c50b125e3a0980cfb931b2aa0a0e74` | shared candidate | nine Runner `ede6756` |
| TboxAgent | 0.1.10 | `ad13cc99f148cf1f8c58e293c68c3461c0571795` | shared candidate | nine Runner `ede6756` |
| WeKnoraAgent | 0.1.11 | `30ec814823662d11cd212dcd9f00a39be0b2cf2b` | shared candidate | four Runner `8bb55ec` |
| DifyDatasetsConnector | 0.1.8 | `009b4b50a18f9fb6a78e4c6778f456d1b471bf4f` | dedicated only | KE `ee265c0` |
| FastGPTConnector | 0.1.6 | `5137a69f66fc32596c7c93e84a6282b053a03769` | dedicated only | KE `ee265c0` |
| LangRAG | 0.1.14 | `ba7bda911c3101c8beb698734da3d80969017486` | shared candidate | KE `ee265c0` |
| LongTermMemory | 0.2.6 | `bf0eda05526fbdb9bf8bd26d914d1474a33a2724` | dedicated only | KE `ee265c0` |
| RAGFlowConnector | 0.1.7 | `b6ab4309cb6387bcc4f20e660aee9a603f57b9fc` | dedicated only | KE `ee265c0` |

Eleven source candidates declare both `shared-runtime-v1` and `stateless-v1`; seven stay dedicated (three native coding Runners and four KE). None is asserted certified or production-shared. The source tree identities include integration-only SDK requirement corrections: the four Runner candidates from `8bb55ec` now require `langbot-plugin>=0.7.4,<0.8`, and LangRAG now requires the same instead of `==0.6.1`; their source trees therefore intentionally differ from the lane tips. CI and contract corrections later changed some source trees; the per-plugin tree column identifies the current PR head, not the earlier lane tips.

**Evidence classes:** The nine-Runner `probe_two_bindings.py` uses a real SDK Worker and synthetic Host/vendor fixture; its synthetic artifact digest is not a built archive hash. The KE `shared_subprocess_probe.py` and checked-in JSON evidence use a real SDK child and local Host vector/storage/embedding fixtures; LongTermMemory temporarily overrides its manifest only inside the probe and remains dedicated. These demonstrate only the operations actually invoked, not real vendor/provider execution. RunnerDemo additionally has exact unsigned archive `f790d29da6d7d8461df35ffc7594eb73cd5d0e100e61e86264ffe843587703ba` from its *earlier lane* and real SDK 0.7.4 Worker dual-binding proof with controlled Host fixtures; it is not the archive of this integrated PR head. The four-Runner lane does not provide real provider acceptance. Real provider-account invocation, signed/downloaded ZIP identity, independent review, production sandbox/network policy, and two real Cloud Workspace installations sharing the same certified digest remain unproven. External KE connector ingest/delete/cancellation and LongTermMemory representative write paths are still release gates; do not interpret a successful local fixture or a marketplace badge as production proof.

No archive was built or published from this integration head. Before any release, build each candidate from the exact committed source and record its immutable archive digest, dependency preparation, certificate fields/signature, downloaded public artifact readback, provider invocation, and two-binding Cloud object/PID/authority evidence separately. No public or production writes were performed by this integration.
