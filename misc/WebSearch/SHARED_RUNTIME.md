# Shared runtime source candidate (SDK >=0.7.4)

This source declares `shared-runtime-v1` and `stateless-v1`. It is not certified
and makes no claim of live vendor acceptance. Certification additionally requires
independent review, signer approval, and two-Workspace invocation acceptance.

- `spec.config` is empty; the tool derives everything from its arguments. No
  configuration is read, cached, or retained on the component object.
- The whole `mux.process` call runs in a worker thread, so the blocking
  fetch/parse cannot stall the shared event loop. At most
  `MAX_CONCURRENT_FETCHES` fetches are in flight per event loop, a call waits at
  most `FETCH_SLOT_WAIT_SECONDS` for a free slot, and cancelling the invocation
  sets a stop flag that ends the I/O. The slot has a single owner: the caller
  releases it if the executor submission itself fails, and the worker thread
  releases it in its own `finally` once it has really exited. An
  await-cancellation cannot cancel the queued executor future (it is awaited
  under `asyncio.shield`), so a worker that has not started yet still runs and
  releases the slot exactly once; a worker that is still running is never
  released early.
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
- the whole operation has an absolute deadline plus a cooperative stop flag that
  cover DNS, connect, the TLS handshake, status/header parsing and the body: a
  watchdog thread shuts the socket down when either fires, because a socket
  timeout only bounds the idle time between two reads. The socket is closed on
  every exit path;
- an aborted, timed-out or truncated response is never returned as a document:
  every exit path re-checks the stop flag and the deadline (a shut-down socket
  can read as a clean EOF), and a body shorter than its declared
  `Content-Length` fails with `TruncatedResponseError` instead of being parsed;
- the socket is opened directly, so no proxy environment variables or netrc
  credentials are consulted, and TLS keeps the default verification context;
- userinfo, query string and fragment are stripped from every URL before it
  reaches an error result, an exception (including chained causes) or a log
  line.

## Limits

- A DNS lookup that the resolver never answers is abandoned on a helper thread:
  the fetch returns at its deadline and gives the slot back, while the stalled
  resolver thread dies on its own.
- A cancelled invocation still cannot un-do I/O that has already been issued,
  but the stop flag and the absolute deadline shut the socket down, so an
  abandoned worker ends at the next blocking-socket return (bounded by the
  watchdog poll interval) instead of waiting for the peer to finish.
- Fetch concurrency is bounded by the plugin's own slot pool, so a tenant cannot
  occupy the default executor without limit.
- Tests drive the real fetch path against a local HTTP server (target refusals,
  redirect re-validation, DNS pinning, TLS context, byte/parse caps, deadline,
  cancellation, a peer that never completes its response headers, the slot
  ownership cases and the redaction of error results/logs); they do not fetch
  the public internet.
