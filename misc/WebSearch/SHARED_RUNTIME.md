# Shared runtime source candidate (SDK >=0.7.4)

This source declares `shared-runtime-v1` and `stateless-v1`. It is not certified
and makes no claim of live vendor acceptance. Certification additionally requires
independent review, signer approval, and two-Workspace invocation acceptance.

- `spec.config` is empty; the tool derives everything from its arguments. No
  configuration is read, cached, or retained on the component object.
- The whole `mux.process` call runs in a worker thread, so the blocking
  fetch/parse cannot stall the shared event loop. At most
  `MAX_CONCURRENT_FETCHES` fetches are in flight per process, a call waits at
  most `FETCH_SLOT_WAIT_SECONDS` for a free slot, and cancelling the invocation
  sets a stop flag that ends the download at the next chunk boundary (the slot
  is released only when the worker thread really exits).
- No binding-keyed caches or detached tasks exist, so
  `on_installation_revoked()` has nothing to release.

## Site adapters

`components/tools/sites/model.py` keeps the module-level `__site_adapters__`
registry. It holds only artifact-level data registered by the `@site(...)`
decorator at import time: each entry is a URL regex plus a class reference. It
contains no tenant, Workspace, or installation data, which the stateless
component contract permits for a process-level cache.

All adapters remain synchronous — they fetch through
`SiteAdapterBase.get_html`, which delegates to `components/tools/sites/safe_fetch.py`.
That is acceptable here because the entire `mux.process` adapter dispatch
(registry lookup, fetch, and parse) is executed in a worker thread by the tool;
no adapter code runs on the event loop. The adapters:

- `SiteAdapterBase` — generic fallback used for every URL in this revision.
- `GithubRepoSiteAdapter`, `GithubUserSiteAdapter` — defined under `sites/github/`
  with `@site(...)`, but that package is not imported by the current registry, so
  they are not registered at runtime (unchanged pre-existing behaviour).

## Outbound target and size policy (`safe_fetch.py`)

Fetching is not a bare `requests.get` any more; the module enforces the
certification requirements in the plugin request layer:

- only `http`/`https` URLs are accepted;
- the host is resolved by the plugin, every resolved address must be globally
  routable (loopback, link-local, private, multicast, reserved, unspecified,
  NAT64/IPv4-mapped forms are refused), and the connection is made to the
  validated IP so a later DNS answer cannot rebind it;
- redirects are followed manually, with the same validation applied to every hop;
- the body is read in bounded chunks: a declared or streamed body above
  `MAX_RESPONSE_BYTES`, and a body that inflates beyond it, abort the fetch
  without being buffered; the HTML handed to BeautifulSoup is capped at
  `MAX_PARSE_CHARS` and the extracted title/brief lengths are clamped;
- the whole operation has a deadline plus a cooperative stop flag, and the socket
  is closed on every exit path;
- the socket is opened directly, so no proxy environment variables or netrc
  credentials are consulted, and TLS keeps the default verification context.

## Limits

- A cancelled invocation cannot interrupt a thread that is already inside a
  socket read; the stop flag plus the total deadline bound how long the
  abandoned worker can keep its slot and socket (see above).
- Fetch concurrency is bounded by the plugin's own slot pool, so a tenant cannot
  occupy the default executor without limit.
- Tests drive the real fetch path against a local HTTP server (target refusals,
  redirect re-validation, DNS pinning, TLS context, byte/parse caps, deadline,
  cancellation and the concurrency bound); they do not fetch the public
  internet.
