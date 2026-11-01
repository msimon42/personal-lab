# Adding a toolset

Start from the actual user outcome. A new domain owns its models, services,
adapters, tests and effect declarations. Reuse plain functions where sufficient;
a small calculation does not need a database, job or approval flow.

1. Add a domain package, or an independent language/project environment when
   dependencies conflict with the Python workspace. Keep experiments outside
   default discovery and dependency resolution.
2. Define strict input and concrete output models plus application operations.
   Return bounded pages or authorized artifact references for large data. Record
   honest coverage and unsupported behavior.
3. Return `OperationSpec` entries from a thin toolset factory. Keep SDK types in
   the host adapter. Declare effects and required filesystem roots, network
   destinations, credentials and recovery behavior. External effects use exact
   application plans and trusted grants.
4. Add one reviewed static registry entry and an explicitly selected profile.
   Never import code from tool arguments, source metadata or arbitrary paths.
5. Add direct service tests, one real MCP subprocess workflow, negative authority
   and scope cases, and idempotency/concurrency/recovery tests for writes.
6. Regenerate contracts with `uv run python scripts/generate_contracts.py`, run
   `uv run --extra dev python scripts/check.py`, and document one agent recipe.

The test-only text-statistics toolset demonstrates this seam without importing
EU5/CK3. It remains disabled in normal profiles. Core may not import any domain or
SDK. Domain packages may not import MCP, FastAPI or host-specific adapters.
Shared infrastructure is extracted only when an actual second use needs it.
