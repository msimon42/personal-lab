# Migration inventory

Baseline observed 31 October 2026 before architecture changes. The recoverable checkout snapshot is `/private/tmp/personal-lab-pre-migration-20261031T193716Z` (225 tracked/untracked files, per-file SHA-256 manifest, binary working-tree patch, and HEAD identity). Existing uncommitted CK3 work is part of this baseline.

## Implemented at baseline

- Deterministic lossless parser, loading profiles, analysis, preservation-aware planner, complete affected-obligation gates, staging, experimental bundles, managed release journals/recovery, and exact rollback. Production promotion remains unavailable.
- Twenty executable synthetic fixture families F01–F20 and physical-directory adapters. Synthetic success is not game conformance.
- Thirty-four CK3 MCP tools with saved source snapshots, indexed contributions, bounded paging, knowledge, complete concern accounting, disclosure policies, signed native user choices, inactive call-allies preparation and guarded inactive undo. Game tests/activation/live undo remain unavailable.
- Official MCP SDK 1.30.0; stdio forwards operations through a detached authenticated loopback FastAPI engine. HTTP handlers are mostly thin, but command validation is HTTP-bound and ordinary decisions assume an operator UI.
- SQLite generic records/events/requests schema, version 1, optimistic revisions and atomic transactions; immutable SHA-256 blobs; guarded filesystem capabilities; complete consistent backups and checked fresh-root restore.
- Headless wheel/sdist delivery preserves CK3 schema bytes, complete implementation fingerprints, workflow receipts, and installer recovery. React and browser workflows are still in the old default CI/product shell.

## Migration risks

`Service` eagerly initializes game-specific resources and a synthetic workspace; `core-only` needs independent composition. Its execution queue was unbounded and cancellation memory-only. Existing backup restore rebases only artifact paths, so it does not establish safe CK3 relocation. Existing intent recording labels plain choice payloads as user decisions; new MCP operations must preserve proposal versus trusted-user provenance.

Live local state is under `.workspace/ck3-verification` and `.workspace/ck3-isolation-qualification`. These are distinct libraries. Do not merge their SQLite databases. Source game/mod roots remain external and read-only. Immutable probe plans contain old absolute paths and must become historical after relocation, never silently rewritten.

## Requirement mapping

The supplied headless amendment governs transport/composition and retires React, REST, browser settings, SSE UI and mandatory internal inference. The newer repository's 20 fixtures, CK3 behavior, exact-byte resource packaging, native choice receipts, disclosure protections, and preservation gates remain requirements. The original `specs/` package and `schemas/ck3` source bytes remain unchanged.

Useful UI assertions to retain in headless replay: exact merged bundle bytes and unchanged originals; explicit narrow conflict choices; malformed input refusal; unknown obligations blocking export; source updates refusing stale export without new artifacts; selected CK3 playset import/advisory findings without activation.

## Commands actually executed before migration

- `UV_CACHE_DIR=/private/tmp/eu5-uv-cache /private/tmp/eu5-ck3-tooling/bin/uv run --extra dev pytest`: **689 passed**, one Starlette/httpx deprecation warning, 56.39 seconds. Temporary roots and disposable native controls only.
- Same uv executable, `run --extra dev ruff check backend tests`: passed.
- Same uv executable, `run --extra dev python scripts/generate_contracts.py --check`: passed.
- `npm test` in frontend with non-login shell and Node 22.22.0: **14 passed**. Initial login-shell run selected an incompatible Node and failed during Vitest startup; corrected environment passed without source changes.
- `git diff --check` and `git diff --exit-code -- specs schemas/ck3`: passed.
- Python observed **3.12.16**. No browser end-to-end, native Windows, game launch, or actual-game conformance executed during planning.

Migration results and remaining gates are maintained in `IMPLEMENTATION_STATUS.md`. This inventory records the actual starting state, not claims about completed migration.

## Final inventory additions

The approved CK3 investigation and isolation roots were migrated and backed up outside the checkout. Two older inactive libraries were subsequently discovered: `.workspace/workspace.sqlite3` (EU5 workbench) and `.workspace/ck3-test-qualification/workspace.sqlite3` (an unlaunched probe). Their relocation was initially withheld pending explicit authorization. On 1 November 2026 the user authorized both migrations with separate instances, complete backups and no game launches. They were verified and moved to OS-local `instances/eu5-legacy` and `instances/ck3-test-qualification`. Their original databases and immutable files remain unchanged in separately archived retired copies, with independent verified backups under `pre-monorepo-20261101`. The checkout no longer contains `.workspace`. The EU5 artifact needed an explicit absent-former-root mapping after the checkout rename; only its copied operational path was changed. See `verification/additional-library-relocation.json` and `IMPLEMENTATION_STATUS.md` for the completed checks.
