# Local US Election Data Platform
## Technical specification

**Version:** 1.0 proposed implementation specification  
**Prepared:** November 12, 2026  
**Owner:** Matt Simon  
**Status:** Ready for implementation planning; no data acquisition, repository changes, or computer installation performed by this document.

## 1. Product outcome

Build a local, auditable database of US election results that supports a web UI with election selection, national maps, state drill-down, county and congressional-district results, and candidate detail on hover or click. Users must be able to distinguish a genuine zero, an unavailable result, an incomplete count, and an election that did not occur.

The platform is a repeatable acquisition and curation system, not a one-time scraper. Every published number must be traceable to an archived source and a reproducible transformation. Historical coverage should grow without requiring a schema redesign or inventing missing information.

### 1.1 Initial scope

- Presidential, US Senate, and US House general elections, including special elections and runoffs needed to interpret the general-election outcome.
- Modern statewide and district returns first; presidential county returns next; county/precinct congressional returns where available.
- Latest available 2026 returns, explicitly distinguished from certified results. Availability and certification are jurisdiction-specific and must be checked at acquisition time.
- Historical expansion back through the modern datasets, then earlier elections as sources and boundaries permit.
- Local storage and processing on the owner's computer, with offline viewing after acquisition.
- Source coverage, provenance, validation reports, exports, and reproducible releases.

### 1.2 Explicit non-goals for version 1

- Forecasting, polling ingestion, demographic modeling, voter-file collection, individual voter records, or prediction markets.
- A guarantee of complete county returns for every election in US history.
- Live election-night scraping with second-by-second service guarantees.
- Automatically interpreting incomplete returns as election calls.
- Fabricating historical boundaries or distributing votes across geography using land area.
- Rebuilding the existing application or all of personal-lab as part of this specification.

Governor elections, primaries, state legislatures, and other offices are supported by extensible entities but are separate coverage milestones. Congressional seat composition, incumbency, and party control require separate membership/tenure data; they cannot be inferred solely from contested races.

## 2. Architecture decisions

### 2.1 Recommended implementation

Use a local **SQLite database**, an immutable filesystem source archive, and versioned static map assets. Use a Rust workspace for the new ingestion CLI and local read API if this module is implemented as a Rust project. This is a recommendation for the new module, not a claim that the existing repository has been inspected or migrated. Before integration, inspect current personal-lab conventions and reuse compatible infrastructure.

Suggested Rust components: Tokio for asynchronous I/O, reqwest for HTTP, serde for serialization, clap for CLI, rusqlite or SQLx for SQLite, and Axum for the optional local HTTP service. Select one database library and lock dependency versions. Parsing and boundary conversion may invoke vetted, pinned command-line tools where a mature Rust equivalent is unavailable. Do not introduce a Python runtime merely for straightforward CSV ingestion.

SQLite is suitable because the main workload is read-heavy and locally owned. One orchestrator writes; API readers use separate connections. Spatial rendering uses prepared map assets rather than requiring PostGIS. DuckDB and Parquet are optional export/analysis tools, not another source of truth. Move to PostgreSQL/PostGIS only if measured requirements demand concurrent writers, large spatial joins, or shared network service.

### 2.2 Data flow

```text
Source registry -> discovery -> download -> immutable raw archive
                                      -> parse/staging -> normalize -> validate
                                                                    -> review
                                                                    -> release
                                                                       |      |
                                                            SQLite read API   map assets
                                                                       \      /
                                                                         UI
```

Acquisition and serving are independent. A failed download or import must not interrupt the last published release. A new release is visible only after all required validation and asset generation succeed.

### 2.3 Local layout

```text
<user-selected-data-root>/
  config/                     source manifests and local settings
  archive/sha256/ab/<hash>     immutable downloaded bytes, content-addressed
  manifests/                  retrieval metadata, checksums, licensing records
  staging/<run-id>/            temporary parse output and diagnostics
  db/elections.sqlite         normalized data and release metadata
  assets/<release-id>/         versioned boundaries and map indexes
  reports/<run-id>/            validation and coverage reports
  exports/<release-id>/        optional CSV/Parquet bundles
  backups/                    local backup manifests or configured destination
  logs/                       rotating structured logs
```

Default to the OS application-data directory and allow `--data-dir`. Do not assume a particular laptop, OS, path, free-space allowance, or existing installation. Keep original source files and database files out of application Git. Commit source manifests, migrations, adapter code, small legally redistributable fixtures, and documentation. Backups must include originals and metadata, not just SQLite.

## 3. Source acquisition and coverage strategy

### 3.1 Candidate source registry

These are starting points identified during planning, not a promise that every listed source has complete coverage or unrestricted redistribution rights. The importer must record the exact downloaded version and its actual date range.

| Source | Intended role | Limitations and checks |
|---|---|---|
| [MIT Election Lab data catalog](https://electionlab.mit.edu/data) | Structured modern federal results; presidential counties; House districts; selected precinct data | Inspect each dataset's actual release, schema, license, and coverage. Catalog labels can lag current files. |
| [FEC results](https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/) | Official federal compilations and cross-checks | Publication lags elections; not a live-results provider. Separate primary, runoff, and general records. |
| State election offices and county election offices linked from official sites | Current results, certified canvasses, detailed local returns | Fifty states plus DC and relevant territories use different formats. Endpoints, publication times, and certification rules vary. |
| [OpenElections](https://github.com/openelections) | County/precinct gap filling and source archives | State and year coverage varies; preserve underlying official-source attribution and dataset revision. |
| [House Clerk election statistics](https://history.house.gov/Institution/Election-Statistics/) | Historical federal tabulations, including publications back to 1920 | Many records are PDF tables. Extraction needs independent checking. |
| [ICPSR historical returns study](https://www.icpsr.umich.edu/web/ICPSR/studies/1/summary) | Earlier county returns, particularly 1824–1968 collections | Check access conditions, codebooks, geographic coding, and reuse rights before depending on or redistributing data. |
| [Census cartographic boundaries](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html) | Modern state, county, and congressional geometry | Match the boundary vintage to the election; cartographic simplification is distinct from legal boundary accuracy. |
| [UCLA historical congressional boundaries](https://cdmaps.polisci.ucla.edu/) | Historical district geometry | Check applicable Congress/election dates, completeness, source documentation, and license. |
| [National Archives Electoral College](https://www.archives.gov/electoral-college/2024) | Electoral allocations and actual electoral-vote outcomes | Electoral votes are separate from popular votes. Use the corresponding year, not a hard-coded 2024 page for every cycle. |

For each source, capture publisher, landing page, download URL, expected format, data scope, update method, license URL/text/hash, attribution text, access restrictions, and redistribution decision. An unknown license does not imply permission to publish. Acquisition for private analysis and publication of a public data bundle are separate decisions.

### 3.2 Discovery order

1. Inventory current structured national datasets and their exact coverage.
2. Seed modern state/district and presidential county returns.
3. Compare coverage against a curated inventory of expected contests and reporting units.
4. Add official jurisdiction adapters for current results and unresolved gaps.
5. Add historical collections and carefully reviewed PDF extraction.
6. Only pursue precinct detail when it serves an identified coverage or map need.

The coverage inventory is itself versioned data. A source containing no rows for a race does not prove the race was uncontested or did not occur. Model unavailable, not held, not applicable, and not yet researched separately.

### 3.3 Source selection and conflict resolution

Prefer the relevant certified official canvass when it is available and comparable. Recounts and later legal corrections may supersede an earlier certified file. Structured secondary datasets are useful for bootstrapping and corroboration but must not silently override official totals.

Source priority is jurisdiction- and contest-specific, not a global numeric score that decides every conflict. Store conflicting observations. A resolution record selects one coherent result set or documents a deliberate merge of nonoverlapping reporting units. Do not construct a candidate total by opportunistically mixing incompatible revisions.

### 3.4 Download behavior

- Use documented downloads or APIs before HTML scraping.
- Enforce per-host concurrency and polite rate limits; respect terms, robots guidance where applicable, Retry-After, and access restrictions.
- Use conditional requests with ETag/Last-Modified when supplied, but hash bytes to establish actual content identity.
- Store retrieval timestamps separately from publisher update timestamps and certification dates.
- Stream to a temporary file, enforce size limits, hash, then atomically move to the archive.
- Record HTTP status, MIME type, original filename, content length, redirect destination, and retrieval outcome. Redact authentication material.
- A changed hash creates a new artifact and import candidate. Never overwrite archived bytes.
- A 304 reuses prior bytes; an unchanged hash records another observation without creating duplicate data.
- Follow redirects only to allowed source destinations. Do not bypass authentication or anti-bot controls.
- Network retries use bounded exponential backoff and jitter. Parsing errors are not fixed by endless download retries.

## 4. Domain semantics

### 4.1 Contest identity

An election is a dated electoral event. A contest is a specific office/seat and stage within it. A Senate class election and a same-year special election must have different contest IDs. A runoff is a distinct stage linked to its predecessor. A ranked-choice counting round is a tabulation round within a stage, not a separate election.

Stable canonical IDs must not depend on a candidate's display name alone. Use deterministic namespaced identifiers from canonical natural keys after normalization, with an explicit alias/correction system. Avoid changing published IDs when spelling corrections occur.

### 4.2 Result grain

One observation is:

`source result set × contest × reporting unit × ballot option × tally basis × count round`

A reporting unit can be a whole state, county, congressional district, precinct, or the **county portion within a congressional district**. Contest scope and reporting scope are separate. A state-level House summary is an aggregation across distinct district contests, not a single statewide House contest unless the election actually used that format.

Ballot options include named candidacies, aggregated write-ins, named write-ins, and historically necessary options. A candidacy is a candidate or ticket standing in a contest. Ballot lines can map to the same candidacy for fusion voting. Store source totals and line totals in distinct series so a candidate total is not added to its constituent lines.

### 4.3 Counts, denominators, and status

- Vote counts use signed 64-bit storage with a nonnegative check. Never use floating point for votes.
- SQL NULL means unknown/not supplied; zero means the source explicitly reports zero or a documented complete tally supports it.
- Valid candidate votes, total ballots, undervotes, overvotes, exhausted ballots, registration, and votes cast are distinct metrics. Do not assume they share a denominator.
- Percentage denominators are explicit: valid candidate votes in the selected round, two-party total, ballots cast, or another documented basis.
- Store provenance-backed source certification status separately from ingestion quality, geographic completeness, and result-call status.
- Winner/advancement can be sourced or derived. Derived plurality leaders must be labeled leaders, not certified winners, during incomplete counts.
- Source-provided percentages are observations for checking; calculate displayed percentages from selected counts and declared denominators when possible.
- Missing third-party votes prevent a trustworthy all-candidate percentage even if the top two counts are present.

## 5. Logical database schema

Use SQLite STRICT tables when supported by the chosen minimum SQLite version. Enable foreign keys on every connection. Use TEXT for stable IDs and UTC ISO-8601 timestamps, INTEGER for counts and booleans, and validated JSON only for source-specific metadata. Core join fields must not be hidden in JSON.

The following tables are required. `PK` denotes primary key; all listed relationship IDs are foreign keys unless described otherwise.

### 5.1 Reference and geographic tables

| Table | Important columns and constraints |
|---|---|
| `office` | `office_id PK`, `code UNIQUE`, `name`, `level`, `default_selection_method` |
| `geographic_entity` | `geo_id PK`, `kind`, `canonical_name`; persistent identity independent of a particular polygon |
| `geographic_version` | `geo_version_id PK`, `geo_id`, `name`, `valid_from`, `valid_to`, `source_artifact_id`, `status`; date intervals use inclusive start/exclusive end |
| `geographic_identifier` | `identifier_id PK`, `geo_version_id`, `scheme`, `value`; codes stored as text to retain leading zeros; unique within scheme and relevant validity |
| `geographic_relation` | `relation_id PK`, `child_version_id`, `parent_version_id`, `relation_type`, `valid_from`, `valid_to`, `source_artifact_id`; supports containment and noncontainment crosswalks |
| `boundary_set` | `boundary_set_id PK`, `publisher`, `vintage`, `crs`, `source_artifact_id`, `license_id`, `quality_status` |
| `boundary_feature` | `feature_id PK`, `boundary_set_id`, `geo_version_id`, `source_feature_key`, `geometry_asset_path`, `geometry_hash`, `bbox_json`; one feature may be a multipolygon |
| `reporting_unit` | `unit_id PK`, `kind`, `geo_version_id nullable`, `parent_unit_id nullable`, `label`, `source_identifier`; supports nongeographic absentee/central-count buckets |
| `reporting_unit_component` | `unit_id`, `geo_version_id`, `role`; composite PK; represents exact county/district intersections without pretending either contains the other |
| `party` / `party_alias` | Stable party identity, historical name, source-specific aliases and effective dates; party history must not be reduced to today's two parties |
| `person` / `person_alias` | Stable candidate identity and names; unresolved names receive separate provisional IDs rather than risky fuzzy merges |

Historical splits and mergers are relationships with documented dates, not just renamed FIPS codes. A reporting unit with unknown geometry remains queryable and contributes to totals only in a declared, nonoverlapping aggregation scheme.

### 5.2 Election and contest tables

| Table | Important columns and constraints |
|---|---|
| `election_event` | `election_id PK`, `label`, `start_date`, `end_date`, `date_precision`, `cycle_year`, `jurisdiction_geo_id`, `event_type`; historical events may span dates |
| `contest` | `contest_id PK`, `election_id`, `office_id`, `seat_key`, `scope_geo_version_id`, `stage`, `contest_type`, `selection_method`, `voting_system`, `seats_to_fill`, `predecessor_contest_id nullable`, `term_start nullable`, `term_end nullable` |
| `candidacy` | `candidacy_id PK`, `contest_id`, `display_name`, `candidacy_kind`, `primary_party_id nullable`, `status` |
| `candidacy_member` | `candidacy_id`, `person_id`, `role`; tickets can contain presidential and vice-presidential candidates |
| `ballot_option` | `option_id PK`, `contest_id`, `candidacy_id nullable`, `party_id nullable`, `option_kind`, `label`; aggregated write-ins may have no person |
| `contest_outcome` | `outcome_id PK`, `contest_id`, `candidacy_id nullable`, `outcome_type`, `basis`, `source_artifact_id nullable`, `release_id`, `notes`; winners, advanced candidates, and unresolved outcomes stay distinct |
| `electoral_allocation` | `allocation_id PK`, `election_id`, `geo_version_id`, `allocation_kind`, `elector_count`, `source_artifact_id`; supports district and statewide allocations |
| `electoral_vote` | `electoral_vote_id PK`, `allocation_id`, `recipient_person_id nullable`, `vote_role`, `votes`, `source_artifact_id`, `release_id`; presidential and VP votes are separate |

Primary keys enforce uniqueness, but natural-key constraints must also detect duplicate contest registration. Special/regular elections, Senate classes, election stages, and seats are included in that key. Territorial delegate races are not quietly counted as voting House seats.

### 5.3 Provenance, ingestion, and observed facts

| Table | Important columns and constraints |
|---|---|
| `license_record` | `license_id PK`, `name`, `url`, `text_hash`, `access_notes`, `redistribution_status`, `reviewed_at` |
| `source` | `source_id PK`, `publisher`, `landing_url`, `authority_kind`, `license_id`, `contact_or_documentation_url` |
| `source_artifact` | `artifact_id PK`, `sha256 UNIQUE`, `archive_path`, `byte_length`, `mime_type`, `format`, `created_at` |
| `artifact_retrieval` | `retrieval_id PK`, `source_id`, `artifact_id nullable`, `requested_url`, `resolved_url`, `retrieved_at`, `published_at nullable`, `http_status`, `etag nullable`, `last_modified nullable`, `status` |
| `import_run` | `run_id PK`, `adapter_id`, `adapter_version`, `code_revision`, `config_hash`, `mapping_version`, `schema_version`, `started_at`, `finished_at`, `status`, `environment_manifest_json` |
| `run_artifact` | `run_id`, `artifact_id`, `role`; composite PK |
| `source_result_set` | `set_id PK`, `run_id`, `source_id`, `contest_id`, `revision_label`, `source_status`, `as_of_at nullable`, `certified_at nullable`, `tally_basis`, `geography_partition_key`, `notes` |
| `vote_observation` | `observation_id PK`, `set_id`, `unit_id`, `option_id`, `round_key`, `voting_mode`, `votes nullable`, `value_status`, `source_row_key`, `raw_candidate_label`, `raw_party_label` |
| `metric_observation` | `metric_id PK`, `set_id`, `unit_id`, `round_key`, `voting_mode`, `metric_code`, `integer_value nullable`, `value_status`, `source_row_key`; holds ballots, exhausted ballots, etc. |
| `observation_evidence` | `observation_id`, `artifact_id`, `locator_json`; locators identify CSV line/row key, spreadsheet sheet/cell, PDF page/table, or JSON pointer |
| `validation_issue` | `issue_id PK`, `run_id`, `rule_id`, `severity`, `entity_type`, `entity_id`, `expected_json`, `actual_json`, `resolution_status`, `resolution_note` |
| `curation_decision` | `decision_id PK`, `decision_type`, `entity_id`, `rationale`, `evidence_json`, `author`, `created_at`, `supersedes_id nullable`; append-only corrections and approvals |

Use a non-null sentinel such as `round_key='final_or_only'` and `voting_mode='all'` only when the source actually identifies that semantics. Unknown values use an explicit `unknown` key and must not be silently treated as totals. A unique key on `(set_id, unit_id, option_id, round_key, voting_mode)` blocks duplicate observations. Additional internal partition keys are required if a source legitimately contains several ballot batches with the same dimensions.

### 5.4 Releases and public serving tables

| Table | Important columns and constraints |
|---|---|
| `data_release` | `release_id PK`, `parent_release_id nullable`, `created_at`, `published_at nullable`, `status`, `schema_version`, `manifest_hash`, `notes` |
| `release_selection` | `release_id`, `contest_id`, `unit_id`, `series_key`, `set_id`, `decision_id`; selects the coherent source set used for a unit/series |
| `published_result` | `release_id`, `contest_id`, `unit_id`, `candidacy_id`, `round_key`, `voting_mode`, `votes`, `denominator nullable`, `result_status`, `derivation_id`; composite PK across grain |
| `derivation` | `derivation_id PK`, `rule_version`, `input_ids_json`, `parameters_json`, `output_hash`; explain materialized totals and candidate-line aggregation |
| `coverage_cell` | `release_id`, `contest_id`, `geography_level`, `coverage_status`, `expected_units nullable`, `available_units`, `missing_units_json`, `boundary_status`, `notes` |
| `release_asset` | `release_id`, `asset_id`, `path`, `sha256`, `kind`, `license_id` |
| `active_release` | Single row with `singleton=1`, `release_id`; updated transactionally only after validation and asset readiness |

A `published_result` is a rebuildable serving cache, never the only surviving evidence. Preserve observation IDs or derivation inputs for every materialized result. Avoid enormous JSON lineage arrays for nationwide aggregates: use a `derivation_input` join table when inputs are large.

### 5.5 SQL invariants and indexes

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

-- Representative table fragment; implementation migrations must define all FKs.
CREATE TABLE vote_observation (
  observation_id TEXT PRIMARY KEY,
  set_id TEXT NOT NULL REFERENCES source_result_set(set_id),
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  option_id TEXT NOT NULL REFERENCES ballot_option(option_id),
  round_key TEXT NOT NULL,
  voting_mode TEXT NOT NULL,
  votes INTEGER CHECK (votes IS NULL OR votes >= 0),
  value_status TEXT NOT NULL CHECK
    (value_status IN ('reported','missing','suppressed','not_applicable')),
  source_row_key TEXT NOT NULL,
  raw_candidate_label TEXT,
  raw_party_label TEXT,
  CHECK ((value_status = 'reported' AND votes IS NOT NULL)
      OR (value_status <> 'reported' AND votes IS NULL)),
  UNIQUE (set_id, unit_id, option_id, round_key, voting_mode)
) STRICT;
```

Required indexes: contests by cycle/office/stage/scope; results by release/contest/unit and release/unit/contest; observations by result set and source row key; aliases by source/name; geographic identifiers by scheme/value; coverage by release/office/year via joins or a materialized summary. Validate with actual query plans. Do not index every column speculatively.

Enforce cross-table consistency in application validation and, where practical, triggers: ballot option and result set belong to the same contest; selected units match the series; round semantics match the voting system; published data always references a published or staged release.

## 6. Boundary and map model

### 6.1 Versioning

Map geometry must match the result's historical geography. Congressional district numbers repeat across redistricting plans and states, so `district 1` is not an identity. Key a district by state, plan/vintage, district designation, and applicable dates. State and county geography also changes over time.

Maintain original boundary files and metadata. Produce WGS84 web assets with consistent feature IDs linked to `geo_version_id`. Retain sufficient source precision for regeneration. Use generalized GeoJSON/TopoJSON for small layers; use vector tiles or PMTiles when measured size warrants them. Keep the API contract independent of packaging.

### 6.2 House drill-down

- National House view uses districts for the selected election's plan.
- State drill-down can show districts or county-within-district results.
- A county split across districts must display each relevant contest, not one fabricated county winner across all House candidates.
- A statewide aggregate of House party votes may be offered as a separately labeled summary with explicit inclusion rules. It is not a statewide contest and is distorted by uncontested/multi-member races.
- Precinct-to-district assignment needs election-specific evidence. Geometric intersection alone does not allocate votes to split precincts.

### 6.3 Missing or incompatible geometry

Support numeric results without maps. If accurate historical polygons are missing, show a table and an explicit boundary-unavailable status. A modern reference map may be displayed only as a clearly labeled reference, never presented as the historical election geography. Cross-year swing maps require compatible units or a documented crosswalk; do not subtract unrelated polygons' totals.

### 6.4 Geometry validation

Check CRS, geometry type, invalid rings, empties, duplicates, out-of-range coordinates, joins, and unexpected overlap/gaps relative to the supplied source. Simplification must preserve topology at the chosen layer and avoid deleting small islands or independent cities without disclosure. Test Alaska, Hawaii, DC, island counties, independent cities, at-large districts, and map insets. Inset transforms affect display only, never stored geographic coordinates.

## 7. Import pipeline and adapter contract

### 7.1 Lifecycle

`discovered -> acquired -> parsed -> normalized -> validated -> review_required/ready -> published`

Failures are explicit terminal or retryable states. Resume from the last completed stage using immutable inputs and stage checkpoints. A run records every stage's start, finish, input hash, output hash, and diagnostic summary.

### 7.2 Adapter interface

Each adapter implements:

- `discover(scope, prior_state) -> artifact_candidates`
- `acquire(candidate) -> archived_artifact + retrieval_record`
- `parse(artifact) -> source_rows + row_locators + parse_diagnostics`
- `normalize(rows, mapping_version) -> typed_observations + unresolved_entities`
- `validate(observations, references) -> issues + coverage`

Parsing must not publish. Normalization must not silently repair arithmetic or interpret missing values as zero. Adapters declare source schema fingerprints, supported formats and date ranges, tally semantics, and required reference mappings. A source layout change fails closed into review.

### 7.3 Determinism and idempotency

An import identity derives from artifact hashes, adapter version, mapping version, config hash, and normalization schema version. Rerunning identical inputs must produce identical normalized records and no duplicated facts. New parser or mapping versions produce a new run without destroying the old one. Stable entity IDs remain stable across runs.

Use canonical serialization and stable sort order when hashing outputs. Retrieval time and random run IDs must not change the semantic content hash. A reproducibility manifest pins code revision, tool versions, locale, timezone, input hashes, and transformation configuration.

### 7.4 PDF and OCR handling

Prefer machine-readable tables or native PDF extraction. OCR is an exception for scanned records, with page-level evidence and confidence indicators. AI may propose extraction or mapping corrections but cannot publish unverified results. Review all ambiguous rows and arithmetic discrepancies. For an initial OCR adapter, manually verify a predetermined sample plus every flagged row; acceptance requires documented agreement against source pages and independent totals. Sampling alone is not proof that an entire historical collection is exact.

## 8. Validation and release gates

Validation outcomes are `error`, `warning`, or `info`. Errors block affected partitions. Warnings require an explicit policy or curation decision before publication. Every rule has a version and targeted fixtures.

### 8.1 Structural checks

- Schema/encoding recognized; required fields present; expected files acquired.
- Vote counts nonnegative integers, dates valid, IDs unique, and references resolvable.
- No duplicate fact grain; no unexplained omitted rows; raw row counts reconcile to accepted/rejected/ignored counts.
- Source candidate labels and provenance locators retained.
- No credential or personal voter data unexpectedly present in published exports.

### 8.2 Semantic checks

- No mixing primary/general/runoff, special/regular, voting modes, source totals/detail lines, or RCV rounds.
- Sum ballot lines to a candidacy only under a declared fusion rule; do not add both line and source candidate totals.
- Validate declared totals when comparable. Exact agreement is expected for comparable certified counts; configurable tolerance must not conceal a substantive mismatch.
- Compare county/precinct totals to official state/district totals only when the partition is known complete and nonoverlapping. Track unallocated/central-count buckets explicitly.
- Validate winner/advancement against voting-system rules and sourced outcomes. Do not infer a winner solely from a partial maximum count.
- Historical multi-seat contests may allow multiple votes per ballot; candidate vote sums need not equal ballots cast.
- Percentage sums are checked only where categories are exhaustive and mutually exclusive.

### 8.3 Completeness checks

Use a versioned inventory of expected contests and expected reporting units. Distinguish `complete`, `partial`, `unavailable`, `not_researched`, `not_held`, and `not_applicable`. Report result completeness separately from map completeness. Missing county counts do not invalidate a separately sourced certified statewide total, but must prevent claims of complete county coverage.

### 8.4 Publication transaction

1. Build a candidate release from selected source sets and curation decisions.
2. Run invariant checks and generate coverage, diffs, summaries, and assets.
3. Verify asset hashes and ensure all referenced files exist.
4. Review changes in totals, winners, party mapping, and coverage versus prior release.
5. In one database transaction, write the release's serving records and switch `active_release`.
6. Retain the previous release for rollback and pinned API queries.

Asset files are written before the pointer switch and are immutable afterward. A crash before the switch leaves the old release active; a crash afterward must leave a complete new release. Garbage collection must not remove artifacts or assets referenced by retained releases.

## 9. Difficult historical cases

| Case | Required treatment |
|---|---|
| Pre-17th-Amendment Senate selection | Record legislative selection or another actual method; never synthesize popular votes. |
| Early presidential elector selection | Record selection method and availability; statewide popular-vote comparisons may be unavailable or incomplete. |
| Electoral votes and faithless electors | Preserve actual electoral votes separately from popular-vote winner and expected allocation. |
| Maine/Nebraska split allocation | Represent district and statewide allocation components explicitly. |
| Fusion voting | Preserve party ballot lines; derive candidate totals once using a declared rule. |
| RCV | Preserve round-level counts, exhausted ballots, and the basis of final outcome; do not add rounds. |
| Runoffs | Separate contest stages and link them; do not sum first-round and runoff votes. |
| Uncontested races | Use recorded votes if available; no invented zero opponent or 100% where counts are absent. |
| Write-ins | Distinguish named write-ins from aggregate categories and avoid duplicate attribution. |
| Multi-member/at-large historical House elections | Record seats to fill and voting system; do not force every contest into a modern single-member model. |
| Redistricting and county changes | Version geography and crosswalks; disable invalid direct swing comparisons. |
| Recounts, corrected canvasses, legal changes | Append revised evidence and publish a new release with an explanation. |
| Late or unallocated ballots | Preserve nongeographic reporting buckets; do not distribute them to counties without evidence. |
| Ties, vacancies, canceled elections | Store sourced outcome/status rather than guessing from vote totals. |
| Party realignment and historical labels | Keep original labels and dated aliases; modern party family groupings are optional derived views. |

## 10. Local API and UI contract

### 10.1 Service model

A read-only HTTP API binds to `127.0.0.1` by default. No LAN exposure is enabled automatically. Serve the UI from the same origin where practical. If cross-origin access is needed, allow only explicit local origins; do not use a wildcard to expose local data. Import/admin actions are CLI-only initially.

Every response includes `release_id`, schema/API version, coverage status, source status, and provenance links or IDs. The UI pins a release for a browsing session so a background update cannot mix years' maps and results from different releases.

### 10.2 Endpoints

```text
GET /api/v1/releases
GET /api/v1/elections?office=president&from=2000&to=2026
GET /api/v1/contests?election_id=...&state=...&stage=general
GET /api/v1/contests/{id}
GET /api/v1/contests/{id}/results?release=...&level=county&round=...&mode=all
GET /api/v1/contests/{id}/results/{unit_id}
GET /api/v1/contests/{id}/coverage?release=...
GET /api/v1/geographies/{id}?release=...
GET /api/v1/maps?contest_id=...&release=...&level=district
GET /api/v1/provenance/{observation_or_derivation_id}
GET /api/v1/coverage?release=...&office=...&from=...&to=...
GET /api/v1/health
```

Use bounded pagination and deterministic sorting. Read endpoints accept only allowlisted filters and parameterized SQL. An unknown unit is 404; a known unit with missing results is 200 with explicit missing status and no fabricated candidate rows. Return 409 for incompatible release/boundary selections, and 422 for invalid filter combinations. HTTP errors use a stable machine code plus a readable explanation.

Representative result:

```json
{
  "api_version": "1",
  "release_id": "release-example",
  "contest_id": "contest-example",
  "unit_id": "county-example",
  "boundary_feature_id": "feature-example",
  "coverage": "complete",
  "source_status": "certified",
  "round": "final_or_only",
  "voting_mode": "all",
  "denominator": {"kind": "valid_candidate_votes", "value": 100000},
  "results": [
    {"candidacy_id": "candidate-a", "votes": 52000, "share": 0.52},
    {"candidacy_id": "candidate-b", "votes": 48000, "share": 0.48}
  ],
  "margin": {"kind": "top_two_percentage_points", "value": 4.0},
  "provenance_ids": ["derivation-example"]
}
```

This is illustrative, not real election data. `share` is a fraction; margin is percentage points. API clients must not confuse those units. For counts beyond JavaScript's exact integer range, serialize count values as decimal strings under a documented versioned policy.

### 10.3 Map and comparison behavior

- Join by stable feature IDs, never display names.
- No-data units use an explicit no-data style, distinct from ties and zero votes.
- Legend identifies the selected measure, election stage, count status, and update date.
- Tooltips show vote counts, shares, denominator, source, and coverage limitations.
- Uncontested, missing, and incomplete results are visually distinguishable.
- Comparisons reject incompatible geography or disclose a verified crosswalk and aggregation method.
- Historical party colors are configurable presentation metadata, not hard-coded normalization rules.
- Map assets are cacheable by content hash; API ETags include release and query parameters.

## 11. CLI contract

Proposed commands:

```text
election-data init --data-dir <path>
election-data sources list
election-data coverage --office president --from 2000 --to 2026
election-data fetch --source <id> --scope <manifest>
election-data import --artifact <id> --adapter <id>
election-data validate --run <id>
election-data issues --run <id>
election-data release build --scope <manifest>
election-data release diff --from <id> --to <id>
election-data release publish <id>
election-data release activate <previous-id>
election-data export --release <id> --format parquet --output <path>
election-data backup --destination <path>
election-data backup verify <manifest>
election-data serve --host 127.0.0.1 --port 4317
```

Provide `--json` for agent use, `--dry-run` where meaningful, and stable exit codes: 0 success, 2 invalid input, 3 source/acquisition failure, 4 validation failure, 5 review required, 6 storage/operational failure. Successful partial work returns a structured per-partition report, not a misleading all-complete result. Destructive cleanup requires explicit scope and refuses to delete referenced data.

Optional MCP tools can wrap read-only capabilities, coverage, provenance, and import diagnostics. Mutating imports and publication remain explicit commands until the owner requests an autonomous workflow. Source text is untrusted data and must never become executable agent instructions.

## 12. Operations, security, and backups

### 12.1 Local operations

- One writer process holds a lock for ingestion/publication; readers continue against published records.
- Use bounded batches and short write transactions. Never hold a DB transaction open while downloading.
- Estimate download size when possible and verify free space before large acquisitions; report unknown size rather than inventing an estimate.
- Provide cancellation at safe stage boundaries and clean orphan temporary files after recovery.
- Log structured events with run/source/release IDs; redact secrets and avoid copying full source payloads into logs.
- On startup, run lightweight consistency checks; run full `integrity_check` and foreign-key checks in explicit diagnostics and backup verification.
- Schema migrations are versioned, transactional where possible, and tested against the oldest supported database. Create a verified pre-migration backup for destructive changes.

### 12.2 Parser safety

Treat downloaded files as untrusted. Restrict archive extraction paths, decompressed size, recursion, and number of files. Reject path traversal and zip bombs. Do not execute spreadsheet macros or embedded scripts. Run PDF/OCR tooling with bounded resources and pinned dependencies. Normalize text encodings explicitly and retain original bytes. Source URLs and filenames must not become shell commands.

### 12.3 Backup and restoration

Use SQLite's backup API or an equivalent consistent snapshot; copying only the live `.sqlite` file while WAL writes occur is not acceptable. Bundle the DB snapshot, archive manifest, referenced source files, source configuration, curation decisions, and release assets, or record an independently verified backup location for each immutable object.

Verify hashes, SQLite integrity, foreign keys, active-release assets, and representative API queries after restoring to a temporary directory. Keep at least one backup outside the primary data drive if the owner selects an appropriate destination. Do not silently upload personal files or use a cloud account without authorization. A local copy on the same disk protects against edits, not disk loss.

### 12.4 Scheduled updates

Manual imports are the default. If the owner later enables scheduled refreshes, configure cadence per source and cycle, bounded retries, disk budget, and stop conditions. Do not poll historical datasets as frequently as current canvasses. A refresh creates a candidate release; automatic publication requires an explicit policy for validation and review. Network outages must leave the offline app functional.

## 13. Testing strategy

### 13.1 Test layers

1. Unit tests for adapters, ID generation, date/code normalization, percentages, and aggregation.
2. Small pinned golden fixtures with known source rows and expected normalized facts.
3. Database migration, constraint, and query tests on temporary SQLite databases.
4. Integration tests for fetch/cache/retry behavior using recorded HTTP fixtures or a local test server.
5. End-to-end tests from archived bytes to a released map/API response.
6. Property tests for idempotency, sum conservation, no double-counting, and round/mode isolation.
7. Regression tests for every corrected historical or source-specific bug.
8. Restore tests and simulated crashes before/after publication pointer changes.

### 13.2 Required adversarial fixtures

Include fusion lines plus candidate totals; split counties; duplicate precinct names; leading-zero FIPS; independent cities; unknown geography; RCV rounds; runoffs; Senate special and regular races; uncontested races with absent vote totals; multi-member House contests; repeated names; historical party aliases; 2026 unofficial-to-certified revision; truncated download; changed CSV columns; malformed PDF; null versus zero; central absentee buckets; geographic revisions; withdrawn candidates; and a source correction reducing previously reported votes.

### 13.3 Performance targets

Targets are proposed acceptance budgets to measure on the actual computer, not existing benchmarks:

- Warm state or county result queries: p95 under 200 ms for the modern baseline dataset.
- Cached national map result payload: under 1 second locally, excluding first asset download/render initialization.
- No API request triggers a source download or full-table historical rebuild.
- Memory stays bounded by configured batch sizes; import a large source without loading all raw rows into RAM.
- Reimporting identical inputs produces no additional published facts.

Record hardware, dataset size, index configuration, and cold/warm cache conditions. Revisit these budgets based on measured needs rather than premature sharding or introducing multiple databases.

## 14. Delivery plan and acceptance criteria

### Phase 0: Inventory and integration planning

Deliver source registry, coverage matrix, repository integration plan, selected data directory, disk estimate, and dependency choices. Inspect the existing app's API and identifiers before choosing integration points. Confirm source access and reuse terms. No broad rewrite is necessary to begin.

**Accept when:** exact source versions and gaps are documented; initial coverage promise is bounded; expected contests and boundaries have an identified reference.

### Phase 1: One complete vertical slice

Use 2024 presidential state and county results as the first target, subject to source validation. Acquire originals, parse, normalize, validate, publish SQLite release, serve results, and join a modern map. Include actual electoral votes as a separate sourced dataset.

**Accept when:** every imported result has provenance; state totals are reconciled or documented; missing geography is explicit; rerun is idempotent; offline viewing works; backup restoration succeeds; map joins have no unexplained failures.

### Phase 2: Modern federal baseline and current cycle

Add presidential, Senate, and House general results from 2000 onward where sourced. Include latest available 2026 data with visible status and revisions. Add district geometry and House-specific drill-down semantics. Do not describe the phase as complete county coverage for all offices unless demonstrated.

**Accept when:** expected contests are accounted for as available or explicitly missing/not applicable; regular/special/runoff distinctions work; current-cycle revisions preserve old evidence; publication is atomic; no unreviewed errors enter the release.

### Phase 3: Broader modern history and geographic detail

Expand to 1976 onward as supported by datasets. Fill county/precinct congressional gaps with jurisdiction adapters. Add independently testable per-state acquisition and source-priority rules.

**Accept when:** coverage reports quantify remaining gaps and reasons; each adapter has fixtures and source documentation; exact House county/district intersections are preserved; no land-area vote allocation is used.

### Phase 4: Historical collections

Add earlier publications and historical returns in documented batches, with selection methods and historical geometry. Treat each collection as a curation project with clear scope.

**Accept when:** every batch has codebook interpretation, geography treatment, known limitations, extraction checks, and appropriate licensing decisions. Unsupported periods show missing coverage, not synthetic results.

### Phase 5: Optional expansions

Governor/primary/state legislative data, demographic overlays, crosswalk-based comparisons, hosted distribution, additional analytics, and MCP automation. Each needs its own scope and acceptance tests. Forecasting remains a separate system that can consume released observed data without rewriting it.

## 15. Definition of done

A milestone is complete only when its promised coverage is documented and delivered, not when a parser runs successfully. Required outputs are:

- Working local database and archived original sources.
- Versioned migrations, adapters, manifests, and reproducible import instructions.
- Passing automated tests plus documented manual source review where needed.
- Coverage and validation reports with unresolved gaps visible.
- Read API or an agreed direct integration consumed by the election UI.
- Matched map assets and no-data behavior.
- Verified backup and restore procedure.
- A release manifest linking code, input artifacts, mappings, outputs, and licenses.

## 16. Decisions and uncertainties to resolve during implementation

The owner need not answer these before the initial implementation inventory; inspect available evidence first.

1. Which computer and existing checkout should host the data, and how much disk space is available?
2. What is the existing app's frontend/API architecture, and which identifiers should be retained?
3. Should this module be Rust-native now, or integrate with existing ingestion code first? This specification recommends Rust for new code but does not authorize replacing unrelated modules.
4. Which precise datasets and licenses cover the first milestone, and which 2026 jurisdictions have certified returns at acquisition time?
5. How much county-level congressional detail is worth acquiring before expanding historical years?
6. Will data remain private/local, or eventually be distributed through a hosted app? Redistribution requirements differ.
7. Which historical boundary collections are accessible and compatible with the chosen result datasets?
8. How should the existing UI represent source disagreements, incomplete historical geography, and confidence in extracted records?

The product can begin without resolving the entire historical archive. The safe default is a correct, reproducible modern release, followed by transparent expansion rather than a superficially complete dataset with hidden assumptions.
