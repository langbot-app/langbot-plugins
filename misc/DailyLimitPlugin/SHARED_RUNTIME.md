# Shared runtime placement (`shared-runtime-v1` + `stateless-v1`)

This source declares `execution.sharedRuntime: shared-runtime-v1` and
`execution.componentModel: stateless-v1` in `manifest.yaml`. It is a source
declaration only: it is **not certified**, has no signed archive, and has not been
accepted against a live multi-Workspace deployment. The exact archive still needs
independent review and acceptance runs.

Under shared placement one `DailyLimitPlugin` object and one of every declared
component serve every installation of the artifact digest. Installation
attachments are lightweight contexts; they do not create another object graph.

## Per invocation

- Configuration is read with `self.get_config()` on every invocation; no config
  value, credential or installation identity is stored on the shared object. The
  invocation config is only used to seed an installation's settings the first time
  that installation loads its own row.
- `get_installation_binding()` is read per invocation and never retained beyond
  the call. It is the only authority for which installation's data is served.
- The EventListener, Page and Tool paths all resolve the invoking installation's
  state on entry, so a request can never be answered from another installation's
  cached data.

## Per binding, released on revocation

- `_states` is keyed by the installation scope
  `<instance_uuid>:<workspace_uuid>:<installation_uuid>` and holds that
  installation's `settings`, `sessions` and its async lock. `runtime_revision` is
  deliberately excluded from the token so a worker upgrade does not orphan a
  tenant's data.
- `BasePlugin.on_installation_revoked(binding)` pops exactly that key. It touches
  process-local state only, because no Host API is available during revocation.
- Durable state is written under `daily_limit_state:<instance>:<workspace>:<installation>`.
  The Host row key (`[instance_uuid, workspace_uuid, owner_type, owner, key]`) has
  no installation dimension, so the installation identity has to live in the
  plugin's own key; without it, two installations in one Workspace would share a
  row.
- Dedicated (binding-less) workers keep the legacy `daily_limit_state` key and a
  single in-process state, i.e. their existing behaviour.

## Known limits

- `initialize()` is process-scoped and does nothing: it runs once per worker with
  an empty config and, under shared placement, before any installation is known.
- Settings are seeded from the invocation config when an installation first loads
  and are then owned by that installation's stored row (unchanged pre-existing
  semantics). Editing the plugin config of an installation whose row already
  exists does not override the stored values.
- A shared worker that gets an invocation without a trusted installation binding
  raises instead of falling back to a binding-less key, so such a request is
  refused rather than served with shared state. A route that does not carry the
  installation binding therefore cannot work under shared placement.
- Legacy data migration is not performed: on shared placement a pre-existing
  shared `daily_limit_state` row is no longer read (each installation starts from
  the config seed). Dedicated workers keep reading the legacy key unchanged.
- The per-installation cache is unbounded in size until revocation; the runtime
  revokes on uninstall, disable, Workspace removal and upgrade.
- Revocation is serialized per installation but may overlap with sibling
  installations' invocations; per-installation state is locked, cross-installation
  state is not shared.

## Tests

`tests/test_installation_scoped_daily_limit.py` drives two distinct
`InstallationBinding`s (same instance and Workspace, different installation
uuids) through one plugin object with the installed SDK's `bind_invocation`, over
a synthetic Host KV whose row key mirrors LangBot's. It asserts that a write from
one installation never appears in the other's snapshot, that a fresh object
("restart of the loader") reloads each installation's own row, that revocation
drops only the revoked binding, and that dedicated binding-less workers keep the
legacy key.
