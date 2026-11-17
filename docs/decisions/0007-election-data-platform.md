# 0007: Local election data platform as a Python workspace domain

Status: accepted, 17 November 2026. Implements Phase 1 of
[`docs/elections/ELECTION_DATA_TECHNICAL_SPEC.md`](../elections/ELECTION_DATA_TECHNICAL_SPEC.md).

## Decision

1. **Language.** The spec recommends Rust *if* the module is a Rust project and asks
   integration to reuse personal-lab conventions first. This repository is a Python/uv
   workspace with macOS/Windows CI, `scripts/check.py`, typed `OperationSpec` toolsets
   and an existing `election-model` domain. The new `packages/election_data` package
   (`election-data`) follows those conventions and depends only on Pydantic and the
   standard library. No Python runtime is added that was not already required. Readers
   for XLSX, shapefiles and HTML tables are small bounded standard-library code, so CI
   needs no GDAL or other native tool.
2. **Storage.** SQLite (STRICT tables, foreign keys, WAL) plus a content-addressed
   immutable archive and versioned GeoJSON assets under an operator-selected data root,
   exactly as in spec sections 2 and 5. Original sources and databases stay out of Git.
3. **Serving.** The spec's read API is delivered as typed transport-independent
   operations (`election_data.query`) exposed through the `election-data` CLI and a
   read-only `election_data` MCP toolset in an explicit `election-data` profile, as
   `AGENTS.md` requires. No HTTP service or web frontend is added in this phase. A
   future frontend (planned by the owner) can wrap the same operations with a thin
   localhost HTTP adapter; that requires a separate architecture amendment because
   `docs/architecture.md` currently forbids a web frontend.
4. **Authority.** Acquisition, import, release publication and backup are CLI-only.
   MCP tools accept identifiers and bounded pages, never paths, URLs or credentials.
   The data root comes only from trusted startup configuration (`election_data_root`).
5. **Separation from `election-model`.** Observed results are a separate domain.
   Forecasting can later consume released observations without rewriting them.

## Consequences

- Every published number is derived from recorded observation IDs with file/cell/row
  locators into archived bytes, under committed, append-only curation decisions.
- County detail (secondary) never overrides official state totals; differences are
  published as coverage notes rather than hidden or "fixed".
- Release identity hashes the input runs, curation decisions and the release-rule source,
  so code or decision changes yield a new release and old releases remain for rollback.
