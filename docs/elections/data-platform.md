# Observed election results

`packages/election_data` (`election-data`) implements the
[election data technical specification](ELECTION_DATA_TECHNICAL_SPEC.md) as an auditable local
archive with reproducible releases. Design choices are in decisions
[0007](../decisions/0007-election-data-platform.md) and
[0008](../decisions/0008-federal-baseline-1976-2024.md). This is observed data only; the
separate [`election-model`](README.md) handles polling research.

## What is in a release

| Series | Coverage | Source and authority |
|---|---|---|
| President, state totals | 1976-2024, all 51 contests each cycle | MIT Election Lab state file (secondary); 2024 from the FEC official compilation |
| Senate, state totals | 1976-2024 incl. specials and runoffs | MIT Election Lab (secondary) |
| House, district totals | 1976-2024 incl. specials and runoffs; DC delegate separately | MIT Election Lab (secondary) |
| President, county detail | 2000-2024 | MIT Election Lab county file (secondary) |
| Electoral votes as cast | 1789-2024, 54 of 60 elections | NARA |
| State and county polygons | 2020 vintage, 1:20M | Census |
| Congressional districts | elections 1976-2024 (95th-119th Congress) | UCLA (95th-112th), Census (113th-116th, 118th, 119th) |

The committed manifest (`election_data/manifests/sources.json`) lists exact URLs, allowed
hosts, size limits, licenses and access conditions. Every judgment call is an append-only
decision in `curation-us-2024-president.json` or `curation-federal.json`.

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
uv run election-data coverage --only-gaps
uv run election-data summary 2000
uv run election-data results --state NY --office house --year 2022
uv run election-data compare 2016 2020 --level county --state PA
uv run election-data provenance <derivation_id>
uv run election-data backup create --destination /another/disk/backup
uv run election-data backup verify /another/disk/backup/backup-manifest.json
uv run election-data release activate <previous_release_id>   # rollback
uv run election-data doctor
```

A full refresh downloads about 410 MB (most of it UCLA district files) and needs roughly
1.3 GB under the data root. `import --adapter all` continues past a source that needs review
and exits 5 with a per-adapter report. All commands accept `--json`. Exit codes: 0 success,
2 invalid input, 3 acquisition failure, 4 validation failure, 5 review required,
6 storage/operational failure. Without a guestbook identity, download MIT files in a browser
and archive them with `election-data archive add`.

To rebuild the committed reference table after a source update:
`uv run python scripts/election_data_build_reference.py --data-dir $ELECTION_DATA_DIR`.

### Agent recipe (MCP)

Add `election_data_root = "/path/you/choose"` to the lab configuration file and start
`lab mcp --profile election-data --config <file>`. All nine tools are read-only:

1. `election_data_status`: active release, sources, licenses, limitations.
2. `election_data_coverage`: per office and cycle, what is expected, available and missing, and why.
3. `election_data_contests` with `office`, `year`, `state`: contest IDs, outcomes, electoral votes.
4. `election_data_results` with `contest_id` (and `level` `county` for presidential drill-down):
   votes, shares (fractions), denominator, top-two margin (percentage points), status, notes.
5. `election_data_provenance` with a unit's `provenance_ids[0]`: source rows, line locators, hashes.
6. `election_data_map` with `contest_id`, or `office` and `year` for a national view: the GeoJSON
   asset reference (join on `feature_id`) and per-feature status; `no_data` is distinct from zero.
7. `election_data_president_summary` with `year`: national popular totals by ticket and electoral votes.
8. `election_data_compare`: presidential margin change between two cycles on identical units.
9. `election_data_issues`: validation issues and the decision accepting each warning.

Responses carry `release_id`; pass it back to pin a browsing session to one release.

## Status against the specification's phases

Executed 17 November 2026 in a Linux cloud container on real sources: 97 archived artifacts,
239,188 vote observations, 12,408 contests with published totals, 114,695 published results; release build
about 21 s; warm reads 1-20 ms.

| Phase | Delivered | Not delivered |
|---|---|---|
| 0 Inventory | Source registry with licenses; expected-contest inventory and per-cycle coverage table; disk need measured | A written dependency/integration plan beyond decisions 0007-0008 |
| 1 2024 President | As before; state totals official, 36 of 51 county partitions exact, 47 with named tickets exact | Maine, Rhode Island and Virginia named-ticket differences need the state canvasses |
| 2 Modern federal | President, Senate, House 1976-2024 contest totals; regular/special/runoff distinct and linked; district geometry and district-level drill-down; atomic publication; no unreviewed errors | **2026 returns** (no reachable structured source; certification in progress); reconciliation to official canvasses; sourced winners for Senate and House; county-within-district House view |
| 3 Back to 1976 | Same contest-level coverage back to 1976; coverage reports quantify gaps | Per-state jurisdiction adapters for congressional county/precinct detail (none) |
| 4 Historical | Electoral votes 1789-2024 for 54 elections; coverage statements for every earlier cycle | Popular votes before 1976; 6 NARA pages awaiting review (1800, 1808, 1816, 1820, 1868, 1936); district geometry before the 95th Congress; House Clerk PDF extraction; ICPSR |
| 5 Optional | Identical-unit comparisons; national presidential summary; coverage and other read tools over MCP | Governors, primaries, state legislatures; demographic overlays; crosswalk-based comparisons; hosted distribution; HTTP API and web viewer |

### Known gaps the inventory reports

- **Missing contests:** Senate North Dakota 1980 and Connecticut 2022 are absent from the MIT file.
  Everything else expected for 1976-2024 is present; 13 Texas House seats in 1996, 4 in 2006 and
  Senate seats in North Carolina 1986 and Illinois 2010 are recorded only as special elections.
- **House boundaries:** matched for every cycle except 2020 North Carolina (13 districts).
- **County detail (President):** states whose county sums equal the state total exactly, and states
  where the named tickets match and only the residual "other" category differs:

  | Cycle | Exact | Named tickets match |
  |---|---:|---:|
  | 2000 | 26 | 41 |
  | 2004 | 27 | 36 |
  | 2008 | 30 | 39 |
  | 2012 | 36 | 45 |
  | 2016 | 29 | 45 |
  | 2020 | 25 | 41 |
  | 2024 | 36 | 47 |

  The largest named-ticket differences are New York (fusion lines: 155,574 in 2004, 262,035 in
  2012) and Alaska 2004 (101,378; the MIT codebook warns its district returns overstate votes).
  County results for 2000-2016 are joined to 2020-vintage polygons and labeled `reference_vintage`.
- **Five House contests** carry a source total that differs from its listed options or a missing
  count; they are published with that note.

### Source findings worth knowing

- The served House file wraps each record in one quoted field with backslash-escaped quotes (987
  records have nicknames or commas); it is decoded by a fixed rule that fails closed.
- Senate special elections cannot be assigned to a seat by rule: California 2022 and 2024, Illinois
  2010 and North Carolina 1986 were specials for the same seat as that day's regular election.
- Georgia Senate runoffs are coded three different ways across cycles; a reviewed stage map
  normalizes them.
- UCLA shapefiles do not follow the usual ring-direction convention, so rings are nested by
  containment; a district redrawn during a Congress appears twice and the plan in force when that
  Congress was elected is used.
- OpenElections' Pennsylvania 2024 precinct file was examined as a first per-state source and not
  adopted: 64 of 67 counties, inconsistent candidate spellings between counties, blank House
  districts in many rows and no license file.

## Limitations

- MIT state-level and county files are secondary compilations; nothing here has been reconciled to
  official state canvasses except 2024 President state totals (FEC).
- Senate and House outcomes are derived plurality leaders, not certified winners; ranked-choice
  contests (Maine from 2018, Alaska from 2022) report a tally whose round the source does not state.
- Electoral allocations attach to the modern state entity for lookup and imply no historical boundary.
- Census 1:20M and grid-snapped UCLA polygons are display generalizations; NAD83 is used as WGS84.
- Candidates other than presidential nominees have no identity across contests.
- No HTTP API or web viewer exists yet (decision 0007); the future frontend can wrap these operations.
