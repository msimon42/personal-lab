# 0008: Federal baseline 1976-2024 and the coverage inventory

Status: accepted, 17 November 2026. Extends [0007](0007-election-data-platform.md) from one
election to President, Senate and House general elections 1976-2024, electoral votes from
1789, and congressional district geometry. Covers Phases 2-3 of the specification at contest
level and parts of Phases 4-5; see [operations and coverage](../elections/data-platform.md)
for what is and is not delivered.

## Decisions

1. **One generic release engine.** A release covers every imported contest. Contest totals
   come from the highest-priority source that reports the contest (FEC official compilation
   for 2024 President, otherwise the MIT Election Lab state-level files); a lower-priority
   source's observations stay stored and are never merged in. County detail is a second
   series reconciled against the selected contest total.
2. **Expected contests are an inventory, not an inference.** `reference-federal.json` is
   derived by `scripts/election_data_build_reference.py` from hash-pinned artifacts: House
   seats per state from the Census apportionment file (each census must sum to 435) and
   Senate classes from regular elections in the source (two per state, 33/33/34). Each
   release compares what it publishes with that inventory per office and cycle and stores
   the result in `coverage_inventory`, including cycles with nothing: before 1976, Senate
   elections before 1914 (legislative selection, not applicable) and 2026.
3. **Seat identity is curated where it cannot be derived.** A Senate special election can be
   for the same seat as that day's regular election, so its class is a reviewed table (one
   entry per special election, each checked against a later regular election for that
   seat). My first rule ("the state's other class") was wrong; it remains in the decision
   log, superseded by `dec-fed-senate-special-seats`, because decisions are append-only.
4. **People are not guessed.** Presidential nominees are matched to curated tickets by
   surname plus a given-name token; every other candidate keeps the source's label as a
   contest-scoped candidacy with no cross-contest person identity. Party lines are separate
   ballot options and a candidate's total is their sum, counted once.
5. **Source formats are decoded by fixed rules that fail closed.** The House file is served
   with each record wrapped in one quoted field; Senate counts are written `7310.0`; `-1`
   means missing. Anything else stops the import for review.
6. **County totals follow one rule for every cycle**, replacing the 2024 state lists: use
   TOTAL rows; else a single reported mode; else sum modes only when the sum equals the
   file's own total for the unit. Repeated rows are summed under the same condition.
7. **Districts are versioned per Congress.** A House result joins only to the districts of
   the Congress it elected: Census files for the 113th-116th, 118th and 119th, UCLA files
   for the 95th-112th (snapped to a 0.01 degree grid). The 2020 election uses the 116th
   except North Carolina, which is published without geometry. No district identity is
   asserted across Congresses beyond what UCLA's own shape identifiers state.
8. **Outcomes stay honest about what is sourced.** Senate and House winners are not in any
   imported source, so those contests report a derived plurality leader, labeled as such,
   with notes for runoffs and ranked-choice contests whose reported round is unknown.
   Presidential elector outcomes come from NARA.
9. **Run hashes cover facts only.** Identifiers, row keys and evidence locators are excluded, and a
   release's identity uses those hashes, so a byte-different copy of a source that states the same
   facts is archived and imported as evidence without creating a new release.
10. **Assets are content-addressed.** Map assets live under `assets/by-hash/` and are shared
   by releases; a release records kind, path and hash.
11. **Comparisons use identical units only.** The comparison operation covers presidential
    margins on units with the same identifier in both cycles; it applies no crosswalk and
    refuses when no unit is shared.

## Consequences

- Schema version 2 (`0002_federal_baseline.sql`) adds `coverage_inventory`, a
  `reference_vintage` boundary status and lookup indexes; version 1 databases migrate in place.
- Adapter versions were bumped where parsing changed, so earlier runs remain as evidence and
  new runs supersede them.
- Congressional county and precinct detail, official-canvass reconciliation of the MIT
  compilations, 2026 returns and pre-1976 popular votes are explicitly open.
