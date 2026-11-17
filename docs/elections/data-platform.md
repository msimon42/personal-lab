# Observed election results: Phase 1

`packages/election_data` (`election-data`) implements Phase 1 of the
[election data technical specification](ELECTION_DATA_TECHNICAL_SPEC.md): the 2024
presidential general election at state and county level, actual electoral votes, and
modern map geometry, as an auditable local archive with reproducible releases. Design
choices are recorded in [decision 0007](../decisions/0007-election-data-platform.md).
This is observed data only; the separate [`election-model`](README.md) handles polling research.

## Sources

| Source | Role | Authority | License / access |
|---|---|---|---|
| FEC *Official 2024 Presidential General Election Results* (`2024presgeresults.xlsx`, compiled 2025-01-16) | Published **state totals** | Official compilation of state-certified returns | US government work; public download |
| MIT Election Lab *County Presidential Returns 2000-2024* (Dataverse v20.0, version `20260225`) | **County detail** | Academic secondary compilation | CC0 1.0; Dataverse guestbook required per download |
| NARA *2024 Electoral College Results* | **Actual electoral votes**; ME/NE district allocation notes | Official certificates | US government work |
| Census 2020 cartographic boundaries, 1:20M (state, county) | Map geometry | Official reference geometry | US government work |

The committed manifest (`election_data/manifests/sources.json`) lists exact URLs, allowed
hosts, size limits and access conditions. Every judgment call that turns source rows into
published results is an append-only decision in
`election_data/manifests/curation-us-2024-president.json`, with rationale and evidence.

## Operating it

```sh
uv sync --locked --all-packages --extra dev
export ELECTION_DATA_DIR=/path/you/choose        # or pass --data-dir; default is the OS app-data dir
uv run election-data init
# MIT downloads need a Dataverse guestbook response. Put it in the data root, never in Git:
#   $ELECTION_DATA_DIR/config/local.toml
#   [dataverse_guestbook]
#   name = "..."; email = "..."; institution = "..."; position = "..."
uv run election-data refresh                 # fetch + import + build a candidate release (never publishes)
uv run election-data issues --severity warning
uv run election-data release publish <release_id>
uv run election-data results --state PA --level county
uv run election-data provenance <derivation_id>
uv run election-data backup create --destination /another/disk/backup-2026-11-17
uv run election-data backup verify /another/disk/backup-2026-11-17/backup-manifest.json
uv run election-data release activate <previous_release_id>   # rollback
uv run election-data doctor
```

Without a guestbook identity you can download the MIT file in a browser and archive it with
`election-data archive add --source mit-county-president --artifact-key countypres_2000-2024.tab FILE`.
Dataverse has no conditional GET, so `fetch` first reads Dataverse's public file metadata and
skips the download (and the guestbook submission) when it is unchanged.

All commands accept `--json`. Exit codes: 0 success, 2 invalid input, 3 acquisition failure,
4 validation failure, 5 review required, 6 storage/operational failure.

### Agent recipe (MCP)

Add `election_data_root = "/path/you/choose"` to the lab configuration file and start
`lab mcp --profile election-data --config <file>`. Tools are read-only observations:

1. `election_data_status`: active release, sources, licenses, coverage counts, limitations.
2. `election_data_contests` with `state` → contest IDs, sourced outcomes, electoral votes, coverage cells.
3. `election_data_results` with `contest_id` and `level` (`state`/`county`) → paged units with votes,
   shares (fractions), denominator, top-two margin (percentage points), status and notes.
4. `election_data_provenance` with a unit's `provenance_ids[0]` → source rows, batch keys,
   file line/cell locators, artifact SHA-256 and license.
5. `election_data_map` → the release's GeoJSON asset reference (join on `feature_id`) and per-feature
   status (`no_data` is distinct from zero and ties).
6. `election_data_issues` → validation issues and the curation decision accepting each warning.

Responses carry `release_id`; pass it back to pin a browsing session to one release.

## Phase 1 acceptance (executed 17 November 2026 in a cloud container)

| Criterion | Result |
|---|---|
| Every imported result has provenance | Every `published_result` row has a derivation whose inputs are observation IDs with line/cell locators into archived bytes. |
| State totals reconciled or documented | State results are the FEC official totals. County partitions equal them exactly in **36 of 51** states; the other 15 are published with the difference disclosed (table below). |
| Missing geography explicit | Alaska's 41 house-district units are tabular with `boundary_unavailable`; Maine/Nebraska district popular votes are `unavailable`. |
| Rerun is idempotent | A second `refresh` produced `unchanged`/`not_modified` retrievals, `already_imported` runs and `unchanged_inputs` for the release; fact counts were identical. |
| Offline viewing | All reads use local SQLite and assets; no request triggers a download. A web viewer is not part of this phase. |
| Backup restoration | `backup create` + `backup verify` restored to a temporary root and passed hashes, `integrity_check`, foreign keys, active-release assets and a sample query. |
| Map joins | 3,154 MIT units → 3,151 county polygons matched via FIPS, one crosswalk (SD 46113 → 46102) and one merge (Jackson County + Kansas City); no unexplained join failures. |
| Electoral votes separate | 538 electors; NARA state rows reconcile to its Total row (Harris 226 / Trump 312) and to the FEC electoral columns. |

Live run: 6 artifacts (49 MB), 20,734 vote and 6,114 metric observations, 12,933 published
results, 56 contests (51 statewide elector contests plus ME CD1-2 and NE CD1-3), build in
about 1 s, warm state/county reads 2-5 ms.

### Where county detail differs from official state totals

County sum minus FEC official total. In 11 of these states the named candidates match
exactly and the difference is only in the residual "other"/write-in category, which MIT
aggregates differently (the MIT codebook reports Democratic, Republican and Libertarian
candidates and groups the rest).

| State | All options | Harris | Trump | Oliver | Complete partition |
|---|---:|---:|---:|---:|---|
| AL | -118 | 0 | 0 | 0 | yes |
| AZ | +10,825 | 0 | 0 | 0 | yes |
| CA | -2,797 | 0 | 0 | 0 | yes |
| CO | -1,872 | 0 | 0 | 0 | yes |
| DC | +10 | 0 | 0 | 0 | yes |
| DE | -1,215 | 0 | 0 | 0 | yes |
| GA | -858 | 0 | 0 | 0 | yes |
| ME | -6,569 | -5,310 | -986 | -33 | yes |
| MT | -27 | 0 | 0 | 0 | yes |
| NE | -5,023 | 0 | 0 | 0 | yes (equals FEC scattered write-ins) |
| NM | -41 | 0 | 0 | -2 | no (4 counties lack a count) |
| PA | -24,526 | 0 | 0 | 0 | yes |
| RI | -1,570 | -1,406 | -115 | -3 | yes |
| VA | -20,712 | +1,617 | +988 | +12 | yes |
| WV | -192 | 0 | 0 | 0 | yes |

Maine, Rhode Island and Virginia differ for named candidates; resolving them needs the
state canvasses (a Phase 2/3 jurisdiction-adapter task), not a correction factor.

## Source quirks handled by reviewed decisions

- **Repeated rows.** MIT repeats some rows at identical grain (NC: four unlabeled rows per
  candidate per county; CA/CT `OTHER`; SC failsafe provisional; AZ undervotes). All rows are kept
  as ordered batches (`batch_key`). Vote batches are summed only for reviewed states and only when
  a unit's options equal MIT's own `totalvotes`; metric batches are never summed.
- **Modes.** `TOTAL` is used when present. Blank mode is stored as `unknown` and published as the
  total only for eight reviewed states; SD vote-center-only units likewise. Other mode-only units are withheld.
- **Missing counts** (NM) are `missing`, never zero; affected units publish no shares.
- **Kansas City** (MIT 36000) is merged with the Jackson County authority only because Missouri's
  full partition reconciles exactly.
- **Ranked-choice** (AK, ME) results are first-round tallies; a rule confirms the leader's majority.
- **Connecticut** uses its eight historical counties, hence the 2020 boundary vintage.

## Limitations and next steps

- County detail is secondary and differs from official totals in 15 states (above).
- Census 1:20M polygons are display generalizations; NAD83 is used as WGS84 (≈1-2 m difference).
- FEC write-in versus ballot-line status (workbook italics) is not captured; FEC surname-only
  labels outside the reviewed list become provisional people rather than guessed identities.
- No HTTP API or web viewer yet (see decision 0007).
- Phase 2 (Senate/House 2000+, 2026 returns, district geometry) is not started.
