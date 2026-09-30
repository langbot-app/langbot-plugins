# Shared runtime source candidate (SDK >=0.7.4)

This source declares `shared-runtime-v1` and `stateless-v1`. It is not certified
and makes no claim of live vendor acceptance. Certification additionally requires
independent review, signer approval, and two-Workspace invocation acceptance.

- `spec.config` is empty; the tool derives everything from its arguments. No
  configuration is read, cached, or retained on the component object.
- The whole `mux.process` call runs in a worker thread via `asyncio.to_thread`,
  so the blocking `requests`-based fetch/parse cannot stall the shared event loop.
- No binding-keyed caches or detached tasks exist, so
  `on_installation_revoked()` has nothing to release.

## Site adapters

`components/tools/sites/model.py` keeps the module-level `__site_adapters__`
registry. It holds only artifact-level data registered by the `@site(...)`
decorator at import time: each entry is a URL regex plus a class reference. It
contains no tenant, Workspace, or installation data, which the stateless
component contract permits for a process-level cache.

All adapters remain synchronous — they fetch with `requests` via
`SiteAdapterBase.get_html`. That is acceptable here because the entire
`mux.process` adapter dispatch (registry lookup, fetch, and parse) is executed in
a worker thread by the tool; no adapter code runs on the event loop. The
adapters:

- `SiteAdapterBase` — generic fallback used for every URL in this revision.
- `GithubRepoSiteAdapter`, `GithubUserSiteAdapter` — defined under `sites/github/`
  with `@site(...)`, but that package is not imported by the current registry, so
  they are not registered at runtime (unchanged pre-existing behaviour).

## Limits

- Thread offload means a cancelled invocation does not interrupt an in-flight
  HTTP fetch; it completes in the worker thread.
- Concurrency is bounded by the default executor's thread pool and each fetch
  uses a fixed 10 s timeout.
- Tests stub `mux.process` and the adapter registry; they do not perform live
  network fetches.
