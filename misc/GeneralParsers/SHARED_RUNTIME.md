# Shared runtime source candidate (SDK >=0.7.4)

This source declares `sharedRuntime: shared-runtime-v1` and `componentModel: stateless-v1`.
It is source-reviewed for the multi-tenant contract; it is **not certified** and makes no
claim of live tenant acceptance.

- Configuration (`enable_vision`, `vision_llm_model_uuid`) is read per invocation through
  `self.get_config()`. Nothing from tenant config is copied onto the plugin, the Parser
  component or module globals, so a shared worker is never initialized with another
  installation's settings.
- The installation binding is resolved per invocation with
  `self.get_installation_binding()` and only used to derive a cache key; the binding
  object is not retained after the call.

## Binding-keyed state

- Parser telemetry (filenames, MIME types, extensions, durations, parse errors) is tenant
  data. It is no longer a process-global singleton: `TelemetryRegistry` keeps one
  `ParserTelemetry` per full installation scope
  `(instance_uuid, workspace_uuid, installation_uuid)`.
- `runtime_revision` is deliberately excluded from the scope so a worker upgrade keeps the
  same bucket.
- `GeneralParsersPlugin.on_installation_revoked(binding)` calls `release_telemetry(binding)`,
  dropping that installation's store on uninstall/disable/workspace-removal/upgrade.

## Page scoping

`PageRequest` carries no binding. `ParserObservabilityPage` resolves the current
installation from the task-local SDK binding (`self.get_installation_binding()`), which the
Host sets from the page request's installation context. `/snapshot` and `/clear` therefore
act only on the calling installation's store and never expose another installation's
filenames or errors. When no binding is active (direct, non-invocation use; unit tests) an
isolated "unscoped" store is used — it never receives records from a bound invocation.

## Known limits

- Telemetry is in-memory, per worker. It is not aggregated across workers and is lost on
  worker restart.
- The unscoped store is never merged into a tenant view; it exists only for direct use.
- No claim of certification; independent review and two-Workspace invocation acceptance are
  still required.
