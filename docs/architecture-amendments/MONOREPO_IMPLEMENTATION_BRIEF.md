# Implementation brief: migrate EU5 toward a headless MCP monorepo

Read `HEADLESS_MCP_MONOREPO_SPEC.md` as the governing architecture amendment. Use this brief to start implementation against the actual repository.

## Objective

Turn the existing EU5 application into the first toolset in a personal monorepo of headless, reusable capabilities operated by an external agent via MCP. No web frontend or custom chat application is part of the target product. The external agent supplies planning and conversation; the repository supplies typed tools, deterministic services, evidence, and controlled side effects.

This is an incremental migration, not permission to rewrite working domain code.

## First: inspect the repository

Read the existing instructions, specifications, dependency files, service entry points, tests, database schemas, and configuration. Check the working tree and preserve unrelated changes.

Create `docs/migration-inventory.md` that identifies:

- What is implemented, incomplete, and merely specified.
- Where business logic is mixed with HTTP handlers, frontend assumptions, or internal model calls.
- Existing parser, resolver, validation, staging, source-protection, and rollback behavior that must survive.
- Current local data locations, schemas, and migration risks.
- The actual commands used to establish the test baseline, with pass/fail/unavailable results.

Do not invent existing packages or claim the test suite passes without running it. Keep current useful package names rather than performing cosmetic renames.

## Implement one vertical slice first

Implement phases 0 through 2 of the main specification before attempting every listed tool.

1. Establish independently callable application services for the existing fixture scan and findings workflow. Keep domain logic separate from transport. Retain fixture and real-installation adapters.
2. Introduce only the minimal generic infrastructure required by the slice: typed results, capabilities, bounded artifact reads, persistent scan runs, and configured profiles.
3. Add a Python workspace structure only to the extent needed to package those components. Reuse the existing tested interpreter baseline. Pin dependencies and commit the lockfile.
4. Add an MCP stdio entry point with concrete named tools and explicit input/output schemas, using the tested official MCP Python SDK version. Do not implement a custom JSON-RPC stack.
5. Expose `lab_capabilities`, `eu5_installations_list`, `eu5_mods_list`, `eu5_scan_start`, `lab_run_get`, `eu5_findings_list`, and the bounded detail/artifact reads needed to inspect a real fixture scan.
6. Add a `core-only` profile that starts without importing EU5 code. Add a tiny unrelated test-only toolset to verify the registration seam.
7. Test the actual subprocess transport, not just Python function calls. Confirm stdout contains protocol messages only.

Do not stub a production success response for an unfinished operation. Do not advertise tools until they work. Existing unsupported EU5 domains must remain visibly unsupported.

## Boundaries that must remain intact

Never edit original Workshop or vanilla source files. All generated changes belong in isolated staging and managed outputs. Keep lossless parsing, repeated-key handling, source provenance, pre-edit preservation constraints, and update invalidation.

No internal LLM calls are required for the new default workflow. Retain useful existing OpenRouter code only as an optional adapter until its role is resolved. Do not replace it with a second custom agent framework.

Keep real game execution optional. Mac tests run offline with synthetic fixtures. A recorded log is not proof that a live game command works; a skipped runtime test is not a passed test.

Do not expose arbitrary shell, SQL, Python execution, unrestricted file access, policy editing, or approval issuance as MCP tools. A future deployment operation must enforce plan-bound authorization in application code rather than trusting `approved=true` from a model.

Keep runtime state out of Git. Never share active SQLite files or transactional staging directories across the VM's shared-folder mount. Do not modify existing historical data without a backed-up, tested migration.

## Do not remove behavior prematurely

Retain the old frontend/API temporarily only where needed to maintain a working baseline during extraction. The supported end state has neither. Before deletion, map useful UI-tested behavior to application, MCP, or CLI acceptance tests. Preserve domain assertions, not obsolete browser navigation.

Do not introduce Redis, a distributed worker service, a universal workflow engine, or a plugin marketplace. Do not create speculative shared packages. Keep unrelated future language projects possible without forcing them into Python's dependency environment.

## First delivery acceptance

A clean checkout on macOS must be able to start the MCP server in fixture mode and complete scan -> run status -> findings -> evidence inspection without a browser, Node, game installation, VM, or model API key.

The scan uses the same domain implementation as direct tests. All required preexisting domain tests still pass, or an explicitly documented preexisting failure is distinguished from a regression. Structured results conform to their schemas, large outputs paginate, and unsupported capabilities are reported honestly.

A separate `core-only` launch must work without EU5 configuration or imports. Subprocess tests must cover discovery, a tool call, stderr logging, and shutdown.

At the end, report changed files, commands actually executed, observed results, preserved limitations, and the smallest next migration step. Do not claim the full production migration is finished after the read-only slice.
