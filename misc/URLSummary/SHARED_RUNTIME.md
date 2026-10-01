# Shared runtime source candidate (SDK >=0.7.4)

This source declares `sharedRuntime: shared-runtime-v1` and `componentModel: stateless-v1`.
It is source-reviewed for the multi-tenant contract; it is **not certified** and makes no
claim of live tenant acceptance.

- Configuration (`max_content_length`, `language`, `model`) is read per event with
  `plugin.get_config()`. Nothing from tenant config is copied onto the plugin, the
  `URLDetector` component or module globals. `max_content_length` is range-checked before
  it reaches the fetch (`components/event_listener/url_detector.py`).
- `initialize()` only attaches a logger; no SDK process-global settings are modified and a
  shared worker is never initialized with another installation's settings.

## Per event

- `URLDetector._handle_message` resolves the maximum content length, summary language and
  model for the event it is handling; when no model is configured it falls back to the
  first model returned by `plugin.get_llm_models()` for that invocation.
- `URLSummary.fetch_page` runs the blocking download and the HTML parse in a worker thread
  (`loop.run_in_executor`) so the shared event loop is never blocked; each fetch opens its
  own connection and closes it on every exit path. At most `MAX_CONCURRENT_FETCHES` fetches
  are in flight per event loop, and a call that waits longer than `FETCH_SLOT_WAIT_SECONDS`
  for a slot is rejected instead of queueing without limit. The slot has a single owner:
  the caller releases it if the executor submission itself fails, and the worker thread
  releases it in its own `finally` once it has really ended. An await-cancellation cannot
  cancel the queued executor future (it is awaited under `asyncio.shield`), so a worker
  that has not started yet still runs and releases the slot exactly once; a worker that is
  still running is never released early.
- `URLSummary.summarize` uses `invoke_llm` under the task-local invocation, so model
  authority comes from the active installation. Replies go through the per-event
  `EventContext`.

## Outbound target and size policy (`safe_fetch.py`)

Fetching is not a bare `aiohttp` call any more; the module enforces the certification
requirements in the plugin request layer:

- only `http`/`https` URLs are accepted;
- the host is resolved by the plugin, every resolved address must be globally routable
  (loopback, link-local, private, multicast, reserved, unspecified, NAT64/IPv4-mapped
  forms are refused), and the connection is made to the validated IP so a later DNS
  answer cannot rebind it;
- redirects are followed manually, with the same validation applied to every hop;
- the body is read in bounded chunks: a declared or streamed body above
  `MAX_RESPONSE_BYTES`, and a body that inflates beyond it, abort the fetch without being
  buffered; the markup handed to the parser is capped at `MAX_PARSE_CHARS`;
- the whole operation has an absolute deadline plus a cooperative stop flag that cover DNS,
  connect, the TLS handshake, status/header parsing and the body: a watchdog thread shuts
  the socket down when either fires, because a socket timeout only bounds the idle time
  between two reads. The socket is closed on every exit path;
- an aborted, timed-out or truncated response is never returned as a document: every exit
  path re-checks the stop flag and the deadline (a shut-down socket can read as a clean
  EOF), and a body shorter than its declared `Content-Length` fails with
  `TruncatedResponseError` instead of being parsed;
- the socket is opened directly, so no proxy environment variables or netrc credentials
  are consulted, and TLS keeps the default verification context (hostname + CA store)
  rather than being disabled;
- userinfo, query string and fragment are stripped from every URL before it reaches an
  error message, an exception (including chained causes) or a log line.

## Identity

No upstream/tenant identity is derived; the plugin keeps no per-user state and calls no
identity-scoped Host API.

## Tests

`tests/test_shared_runtime.py` drives one plugin/component object graph across two
distinct `InstallationBinding` values with the real SDK `bind_invocation(...)` and
`InstallationBinding`, and asserts that content-length/language/model are resolved per
event.

`tests/test_outbound_safety.py` drives the real fetch path against a local HTTP server and
covers the blocking findings: non-public targets and non-HTTP(S) schemes are refused before
a connection is made, redirects are re-validated, the connection uses the validated IP,
TLS keeps the default verification context, oversized/compressed bodies are cut off at the
cap, the parse input is capped, the fetch runs off the event loop with a bounded number of
in-flight fetches, the stop flag and the absolute deadline end a download that never sends
a complete status line, a fetch cancelled while queued and a failed executor submission
still release their slot exactly once, a worker that is still running is never released
early, and no error message, log record or traceback repeats the URL's userinfo, query or
fragment. Page content is served locally; no vendor account is used.

## Known limits

- Fetching and summarization are Host-model and network dependent.
- A DNS lookup that the resolver never answers is abandoned on a helper thread:
  the fetch returns at its deadline and gives the slot back, while the stalled
  resolver thread dies on its own. The plugin enforces its own target and size
  policy, but a deployment SHOULD still apply egress policy as defence in depth.
- No claim of certification; independent review and two-Workspace invocation acceptance
  are still required.
