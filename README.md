# Personal Lab

Headless personal tools operated by an external agent through one MCP connection.
The existing EU5/CK3 mod engine is the first toolset. The repository supplies typed
operations, deterministic checks, evidence and controlled effects; your agent
supplies the conversation and investigation strategy.

The supported runtime requires Python, with no browser, Node, HTTP server or model
API key. Game/mod source files remain read-only. New projects join through their
own services and trusted toolset registration.

## Development

Install uv, then:

```sh
uv sync --locked --all-packages --extra dev
uv run --extra dev python scripts/check.py
uv run --package personal-lab-toolkit lab fixtures
```

Python 3.12.16 and the tested dependency lock are pinned. Checks use temporary
synthetic sources; they do not establish game conformance. macOS development and
Windows CI contracts are separate from native Windows deployment and game tests.

Start a fixture-backed MCP server using an explicit local state directory:

```sh
uv run --package personal-lab-toolkit lab mcp --profile dev --state-dir /absolute/local/lab-dev
```

The client launches that command over stdio. Protocol messages are on stdout and
logs are on stderr. Do not run it as a web server or put live SQLite/staging state
on a VM shared folder. Production startup uses an already installed environment;
no tool call installs or upgrades dependencies.

## Agent interface

Use the single `personal_lab` connection. `lab_capabilities` reports enabled
profiles, limits and unavailable operations. Results share a typed envelope;
inspect `data`, `limitations`, explicit continuations and artifact references.

- `lab_*`: capabilities, persistent run status/events/cancellation, bounded artifacts.
- `eu5_*`: scan, findings, source/definition/reference inspection, proposed intent,
  revisioned staging, validation, deployment-plan preparation and experimental ZIPs.
- `ck3_*`: saved playset investigations, source policy, complete concern reviews,
  contributor paging, sourced guidance and reviewed inactive call-allies changes.

For CK3, start with `ck3_get_context`; use `selection="resume"` to continue saved
work without scanning again. Game launch, normal-playset activation and live undo
remain unavailable. Prepared files and synthetic checks do not prove gameplay.

MCP profiles are `personal` (core plus both games), `dev` (EU5 fixtures),
`eu5-local`, `ck3-local`, and `core-only`. Core-only also works when the mod-engine
package is not installed. The unrelated test toolset is disabled in production
profiles. Copy [local configuration](config/profiles.example.toml) outside Git to
register read-only roots; tool requests cannot edit policy or root configuration.

## Installed use and approvals

Build all three distributions with `uv build --all-packages`; distribute the
matching wheels together. Install the toolkit wheel and its core/domain wheel
dependencies into an environment outside the checkout. Run that environment's
`lab install --profile personal --state-dir /absolute/local/personal-lab-state`
to update the owned Codex connection and CK3 operating instructions. Reconnect the
client after an interface update. Unrelated settings and stricter policies are
preserved.

Native trusted responses collect exact operator choices. Hosts without that
channel return a `lab decide <question-id>` terminal route. External actions
require an immutable eligible plan and `lab approve <plan-id>` confirmation.
Neither terminal command is a model-callable approval tool. Intent is separate
from external-action authorization; `approved=true` is never authority.

CLI diagnostics and recovery include `lab doctor`, `lab runs inspect`,
`lab runs events`, `lab backup`, `lab restore`, `lab recover-restore`, and backed-up `lab migrate`.
Restores create a separate instance, preserve investigations, and invalidate old
authority. Backups with unresolved external-write journals require recovery before
restoring; effects are never replayed automatically. The CLI shares the application
boundary and does not start a competing writer.
New state defaults to OS-local `personal-lab/instances/` directories. Relocation
preserves immutable evidence and invalidates old pending write/launch authority.

## Architecture and current limits

Read [architecture](docs/architecture.md), [generated tool contracts](docs/tool-contracts.md),
[adding a toolset](docs/adding-a-toolset.md), [migration inventory](docs/migration-inventory.md),
and [implementation status](docs/IMPLEMENTATION_STATUS.md).

The supplied [headless amendment](docs/architecture-amendments/HEADLESS_MCP_MONOREPO_SPEC.md)
supersedes the former web shell; the original specification package remains
preserved as historical source. All 20 EU5 fixture families and the CK3
preservation/disclosure protections remain required. Zero production game rules
have been conformed. Windows deployment, broader game repairs and runtime
qualification remain separate evidence-backed milestones.
