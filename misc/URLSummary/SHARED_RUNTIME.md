# Shared runtime source candidate (SDK >=0.7.4)

This source declares `sharedRuntime: shared-runtime-v1` and `componentModel: stateless-v1`.
It is source-reviewed for the multi-tenant contract; it is **not certified** and makes no
claim of live tenant acceptance.

- Configuration (`max_content_length`, `language`, `model`) is read per event with
  `plugin.get_config()`. Nothing from tenant config is copied onto the plugin, the
  `URLDetector` component or module globals.
- `initialize()` only attaches a logger; no SDK process-global settings are modified and a
  shared worker is never initialized with another installation's settings.

## Per event

- `URLDetector._handle_message` resolves the maximum content length, summary language and
  model for the event it is handling; when no model is configured it falls back to the
  first model returned by `plugin.get_llm_models()` for that invocation.
- `URLSummary.fetch_page` opens a fresh `aiohttp.ClientSession` (with its own timeout) for
  every fetch and closes it when the call returns; no session or response is shared across
  invocations.
- `URLSummary.summarize` uses `invoke_llm` under the task-local invocation, so model
  authority comes from the active installation. Replies go through the per-event
  `EventContext`.

## Identity

No upstream/tenant identity is derived; the plugin keeps no per-user state and calls no
identity-scoped Host API.

## Tests

`tests/test_shared_runtime.py` drives one plugin/component object graph across two
distinct `InstallationBinding` values with the real SDK `bind_invocation(...)` and
`InstallationBinding`, and asserts (a) content-length/language/model are resolved per
event and (b) each fetch builds its own client session. Page/LLM responses are simulated;
no live network or vendor account is used.

## Known limits

- `fetch_page` disables TLS verification (`ssl=False`) and follows redirects
  (`allow_redirects=True`). This is best-effort page fetching, NOT an SSRF/network
  sandbox; shared deployments MUST enforce their own egress policy against metadata and
  control endpoints.
- Fetching and summarization are Host-model and network dependent.
- No claim of certification; independent review and two-Workspace invocation acceptance
  are still required.
