# Shared runtime placement (`shared-runtime-v1` + `stateless-v1`)

This source declares `execution.sharedRuntime: shared-runtime-v1` and
`execution.componentModel: stateless-v1` in `manifest.yaml`. It is a source
declaration only: it is **not certified**, has no signed archive, and has not been
accepted against a live multi-Workspace deployment. The exact archive still needs
independent review and acceptance runs.

Under shared placement one `ScheNotify` object and one of every declared component
serve every installation of the artifact digest. Installation attachments are
lightweight contexts; they do not create another object graph.

## Per invocation

- The `language` config is read through `self.plugin.get_config()` inside the
  command and Tool components that use it. No config value is stored on the shared
  object or in a scheduled event.
- `get_installation_binding()` is read per invocation and never retained past the
  call; it decides which installation's schedule `add_scheduled_event`,
  `get_scheduled_events` and `delete_scheduled_event` read and write.
- `initialize()` is process-scoped and only logs: it runs once per worker with an
  empty config, before any installation is known, so it starts no scheduler.

## Per binding, released on revocation

- `_events` is keyed by the installation scope
  `<instance_uuid>:<workspace_uuid>:<installation_uuid>` and holds that
  installation's pending reminders only. `runtime_revision` is deliberately excluded
  from the token, so a worker upgrade does not orphan a tenant's pending reminders
  within the process lifetime.
- `_loops` holds one minute-scheduler task per installation scope.
- `BasePlugin.on_installation_revoked(binding)` cancels that installation's
  scheduler (bounded 5 s wait) and drops its pending reminders, so a revoked
  installation can never fire an already-scheduled notification afterwards.
- The schedule is in-memory only (unchanged pre-existing behaviour): nothing about a
  reminder is written to Host storage, so revocation has no durable row to clean up.
- Dedicated (binding-less) workers keep a single binding-less schedule and their
  existing behaviour; their schedulers are stopped by process shutdown.

## The detached scheduler

Each installation's scheduler is a detached task, registered per installation and
stopped by revocation. It carries its installation scope as an explicit argument
and runs in a **fresh context** (`contextvars.Context()`), not the context it was
created in. A task created inside an invocation inherits that invocation's
authority, which is revoked when the invocation returns; every Host call such a
task makes afterwards fails with "Plugin invocation has ended". The scheduler
therefore never reads config or any other invocation-scoped API, and it never
resolves its own binding at delivery time.

## Known limits

- The scheduler is in-memory: pending reminders are lost on process restart (also
  the pre-existing behaviour).
- Delivery is a detached relay. It carries no task-local installation authority at
  all — the worker resolves it from the connection/current control binding, exactly
  as the dedicated path always has — while the schedule and its scheduler are
  already scoped to one installation. A shared deployment that requires
  per-installation authority on a scheduler-originated send must treat that as a
  release gate; this source does not certify it.
- Editing the `language` config only affects messages produced by later
  invocations; notification delivery itself reads no config, because the prefix is
  language-independent (the previous per-language lookup produced the same literal
  for both supported languages and was removed).
- `get_scheduled_events()` returns a copy of the list, so a caller cannot mutate
  another installation's (or its own) schedule in place.
- The per-installation state is unbounded apart from the number of scheduled
  reminders and is only freed by revocation. The runtime revokes on uninstall,
  disable, Workspace removal and upgrade.
- Revocation is serialized per installation but may overlap with sibling
  installations' invocations; per-installation state is isolated, nothing is shared.

## Tests

`tests/test_installation_schedule.py` drives two distinct `InstallationBinding`s
(same instance and Workspace, different installation uuids) through one plugin
object with the installed SDK's `bind_invocation`, over a stub Host that records
notification relays. It asserts that B's `get_scheduled_events` returns only B's
reminder (pre-fix it returned both, from one shared list), that A's scheduler fires
only A's due reminder and leaves B's pending, that the scheduler is registered per
binding and stopped by revocation with the revoked installation's schedule dropped
(no late delivery) while a sibling's schedule survives, that the detached delivery
path reads no invocation config, that `initialize()` starts no scheduler, and that
the scope token is the full binding.
