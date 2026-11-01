# Personal Lab architecture

Personal Lab supplies bounded capabilities to an external agent through stdio MCP. The external agent owns conversation and investigation strategy. Application operations own validation, policy, state transitions and side effects. No model or web server is required.

The accepted amendments in `architecture-amendments/` replace the historical web product architecture. Original game-specific correctness and source protection still apply. The existing mod engine is the first domain; additional products arrive only when requested.

## Dependency direction

- `personal-lab-toolkit` composes core and optional domain packages. It owns registry/profiles, CLI/MCP adapters and verified host confirmation bridges.
- `toolkit-core` supplies transport-independent contracts, SQLite/blobs, guarded filesystem capabilities, bounded runs, scoped artifacts and plan authorization. It imports no game, MCP or host code.
- `election-model` owns public aggregate polling data, research scenarios and retrospective evaluation. It depends only on Pydantic and the standard library; the optional election-research host profile registers its bounded read-only operations.
- The existing `eu5-mod-engine` package retains `backend` imports and pure parser/analysis/planning semantics. Generic core is its only new dependency. SDK/host code moves into the toolkit.

Startup selects trusted lazy factories before registration. `core-only` does not import game code; the test-only text-statistics toolset independently exercises the seam. Future languages or conflicting dependencies may run separate processes from this repository.

## Runtime and state

One process owns execution per state directory. The MCP process directly owns services and the bounded worker; there is no detached HTTP engine. SQLite history survives disconnection; work does not survive process exit. Orphaned work is interrupted and external effects are never automatically replayed.

New local state lives in OS-local `personal-lab/instances/<instance>` directories. Existing IDs and immutable blobs/history remain supported. State directories, environments and active transactions are excluded from Git and VM shared folders.

## Authority and evidence

Configured roots grant filesystem scope; tool calls use identifiers, not arbitrary paths. Observation, internal staged mutation and external effects are distinct. Artifacts retain ownership and disclosure checks at read time. Plans bind exact inputs, candidates, destinations and validation. Native trusted responses or terminal approval issue grants; model parameters cannot.

Coding-agent terminal access under the same OS identity can bypass application policy. The runtime interface does not claim to isolate its OS owner. Game/mod inputs remain read-only. Synthetic fixtures and recorded logs never establish live-game capabilities.

## Contracts and diagnostics

`lab` and MCP invoke the same typed operations. Structured output uses explicit schemas, bounded pages and scope-bound continuations. Logs go to stderr. Profiles and capabilities expose actual support, including unavailable native/game lanes. `tool-contracts.md` is generated from trusted registration definitions.
