# Aggregate U.S. election research module

Snapshot: **1 November 2026**, ahead of the **3 November 2026** House and Senate elections. The independent `election-model` package supplies local typed calculations through the explicit `election-research` profile. There is no frontend, HTTP service, model loop, voter database, campaign targeting, or inference about a demographic group's private political beliefs.

Start with `election_status`: it exposes the four primary-release races and the separate opt-in dataset of22 polls across17 races/15 firms. The [broader feed instructions](#broader-opt-in-polling-feed) show how to select that candidate-party-verified subset without relaxing its QA.

The evidence supports studying polling error, but **does not establish a Democratic polling-error reversal**. Recent generic-ballot releases favor Democrats; that measures stated support, not the difference between polls and the eventual vote. The zero-directional-adjustment baseline remains the recommended scenario because the historical comparison does not show an improvement from a learned signed correction.

## Research findings and treatment

| Facet | Evidence available by the cutoff | Treatment |
|---|---|---|
| Presidential versus midterm errors | AAPOR reports small average signed errors in 2018 and 2022, unlike the repeated Democratic overstatement in recent presidential cycles. Its figures use two-party-margin definitions and a different poll sample from this module. | Train separately by chamber on earlier midterms; never transfer a presidential Trump correction automatically. |
| Turnout versus vote switching | Current likely-voter polls measure a modeled electorate; repeated aggregate snapshots do not identify individual transitions. | Keep switching, participation/composition and measurement error as distinct possible explanations. No hidden-voter classification. |
| Modes, house effects and likely-voter screens | Pew describes the role and limitations of political weighting and nonresponse. Current releases differ in mode and weighting. | Preserve mode/population metadata, retain one latest observation per linked firm/overlap cluster, cap cluster weights. No invented mode or house-effect coefficient. |
| Correlated errors | Multiple surveys can share a nonresponse problem. Poll counts do not measure independent national uncertainty. | Common national/state shocks plus race residuals. The correlation decomposition is a declared sensitivity assumption. |
| Incumbency and the economy | Marist reports 40% presidential approval among likely voters. BEA's October29 vintage reports Q3 GDP growth of3.4%SAAR. September BLS releases report slower hiring and3.7% annual CPI inflation. | Context only; no adequately validated vintage macro/incumbency coefficient. Avoid counting a poll-measured environment twice. |
| Candidates and local effects | The primary seed covers three Senate contests and NY-17, all from Emerson. | Use direct race polls; no unsupported candidate-quality adjustment or extrapolation to unpolled seats. |
| Redistricting | Census documents2026 boundary changes and corrections to some Missouri products. | Require House map labels; no joins of old/new district numbers, prior-margin transfer, or cross-map fundamentals. |
| Timing and undecideds | Kansas/Texas publish initial and leaner versions from the same interviews. Some seed polls are several weeks old. | Use initial ballot once, keep undecideds/other shares, show poll age. Recency weighting alone does not increase uncertainty for a single stale poll; no fitted late-movement model is claimed. |
| Special elections and early turnout | No consistently verified numeric series was obtained for this snapshot. | Missing, rather than zero. No turnout-to-preference conversion or special-election boost. |

Primary references: [AAPOR2024 executive summary](https://aapor.org/wp-content/uploads/2025/10/Task-Force-on-2024-Pre-Election-Polling-Report-Executive-Summary.pdf), [Pew political weighting](https://www.pewresearch.org/methods/2026/09/16/why-many-polls-need-to-weight-on-party-affiliation-or-past-vote/), [Census district vintages](https://www.census.gov/programs-surveys/decennial-census/about/rdo/congressional-districts.html), [Marist national release](https://maristpoll.marist.edu/polls/u-s-midterm-elections-october-2026/), [BEA October29 release](https://www.bea.gov/news/2026/gdp-advance-estimate-3rd-quarter-2026), [BLS employment vintage](https://www.bls.gov/news.release/archives/empsit_10022026.htm), and [BLS CPI vintage](https://www.bls.gov/news.release/archives/cpi_10142026.htm). Numerical context is available in `election_status.context_observations`; no macro values enter race calculations.

## Data and provenance

`packages/election_model/src/election_model/data/` contains versioned public fact extractions, normalized historical aggregates, and a SHA256/byte-size manifest checked at read time. Narrow `.gitattributes` rules disable Git text conversion only for election evidence paths, preserving those exact hashes on Windows checkouts. A temporary-repository regression checks both LF and CRLF source bytes with `core.autocrlf=true`. Source identifiers, URLs, availability dates, retrieval timestamps, sample/population/mode metadata and limitations remain explicit. Whole copyrighted publisher releases are not redistributed. A missing document hash means original document bytes were not retained; it is not a fabricated hash of the source page.

The primary current dataset `us-2026-11-01` contains:

| Race | Field dates | Release | Sample | Initial D/R shares |
|---|---|---|---|---|
| Kansas Senate | Oct26–28 | Oct30 |800LV |49/45 |
| Texas Senate | Oct17–19 | Oct22 |1000LV |48/46 |
| Iowa Senate | Oct5–6 | Oct9 |650LV |45/47 |
| New York17 House | Sep27–29 | Oct1 |400LV |48/46 |

These are attributed [Kansas](https://emersoncollegepolling.com/october-2026-kansas-poll-hamilton-maintains-edge-over-marshall/), [Texas](https://emersoncollegepolling.com/texas-2026-poll-toss-up/), [Iowa](https://emersoncollegepolling.com/iowa-2026-poll-u-s-senate-race-tightens-sand-maintains-edge-for-governor/), and [NY-17](https://emersoncollegepolling.com/ny-17-2026-poll/) release facts. They are a deliberately limited seed, not an exhaustive poll inventory, representative race sample, or chamber universe. Senate IDs distinguish the chosen election contest. NY-17 uses the publisher's2026 geography without transferring a historical district baseline. The dated bundle accepts only its stated cutoff.

Historical data comes from the [FiveThirtyEight pollster-ratings archive](https://github.com/fivethirtyeight/data/tree/master/pollster-ratings), attributed under CC-BY4.0. Original CSV/README/license bytes and retrieval/hash records are retained in `data/elections/sources/`;4,234 selected source questions remain in `data/elections/normalized/archive-polls-v3.json`.

The archive contains20,466 rows across1998–2023 overall. We select D/R House/Senate contests held on the federal November date in midterm years. One row whose rounded D/R shares exceed100% is excluded and counted; special elections held on the same date can remain under separate source race IDs. After recency/availability/clustering rules,749 race aggregates remain:554House and195Senate across1998,2002,2006,2010,2014,2018,2022. A separate28-race eligibility ledger excludes incompatible or ambiguous voting rounds (21Georgia,5Maine,1Alaska,1Mississippi); another58 selected races have no eligible poll. **No2024 row-level history is present.**

This is a retrospectively curated pollster-evaluation archive, not a historical forecast tape. It selects eventual top-two candidates, contains provider-imputed sample sizes, and gives median field dates rather than true field-end/publication dates. Normalization uses an explicitly assumed seven-day median-to-availability lag, a two-day forecast horizon, and an outcome-availability proxy of election+30days. Poll n is held at1000 to avoid the provider's retrospective n imputation. None of these assumptions recovers actual publication history. Official-return reconciliation and a representative full slate remain unavailable.

`history-v3.json` is the eligibility-reviewed default: it excludes incompatible/unknown voting rounds before calibration and retains12-decimal margin rounding. Original `history.json` and `history-v2.json` remain immutable superseded evidence; they are not active calibration inputs. Raw CSV SHA256: `9f0b185506e59117c150fb988e73b612346effe8e3a8f0d90fdafcad07f54756`. V3 history SHA256: `63faee944caa91a654d9519176abbb9a02b5f39dcad9c58f236f8db7ea5f5287`.

## Model

All margins are **Democratic share minus Republican share, in percentage points of the full reported sample**. Undecideds and other choices are not forced into a two-party denominator. The archive's historical actual margins follow its reported share definition. This differs from AAPOR's two-party summary metric.

Polls must be available by the cutoff, end by the cutoff, fall within45days, and belong to a requested future race. Repeated questions, tracking waves, and LV/RV versions linked by firm or declared overlap do not add independent observations. One latest observation per connected overlap cluster remains, with LV preferred on a date tie. Weights are proportional to square-root sample size times a14day half-life. A35% cluster cap redistributes excess mass; with fewer than three independent clusters that cap is infeasible and the output says so. Unknown overlap remains a limitation. The model estimates no current house effect.

When a result's actual sample size is unverified, the numerical sample-size placeholder is1000 and the output warns about it. This is a modeling assumption, **not a verified n or a guarantee of conservative precision**. Retained provider n stays in the input for audit. Simple-random-sample multinomial D−R variance is only a lower-bound calculation; weighting/design effects and nonresponse are not recovered from n.

For each chamber, historical error is `actual_margin − poll_margin`. Cycle means get equal weight; the signed mean shrinks toward zero by `cycles/(cycles+3)`. Residual spread includes within-cycle dispersion and variation in cycle means. Only earlier midterm cycles, outcomes available before the snapshot, and exactly matching forecast horizons enter calibration. Bundled calibration rejects changes to the aggregation rules unless comparably normalized history is supplied.

Three comparisons share the same seeded random shocks:

- `baseline`: no directional error correction; the recommended default.
- `historical`: add the chamber-specific shrunk historical signed error.
- `reversal`: add the explicitly chosen `reversal_shift` to the raw poll margin, default+3D points. It is a hypothetical sensitivity, not an inferred voter movement or learned2026 reversal. Set−3 for a symmetric Republican stress case.

The total standard deviation is at least the historical residual spread, configured4point floor, sampling lower bound, and configured common-error magnitude. Defaults allocate2points to a shared national shock and1point to a state shock; remaining variance is race-specific. These values are sensitivity choices, not fitted2026 correlation estimates. Normal latent margins are approximate; reported margins/interval endpoints are bounded to electoral limits. At5000 draws, maximum probability Monte Carlo standard error is approximately0.007; a stable seed makes reruns identical. No chamber-control probability is produced, and a complete joint seat forecast is not implied.

## Executed historical evaluation

Forward-cycle holdouts use at least two earlier training cycles and test2006,2010,2014,2018,2022. There is no random poll-row split and no retrospective pollster-rating weight. The same uncertainty model is used in both comparisons. These are **retrospective diagnostics**, with the archive limitations above, rather than leakage-free historical replay.

|629 held-out race snapshots | Mean absolute error | RMSE | Brier score |90% interval coverage |
|---|---:|---:|---:|---:|
| Zero-bias baseline |5.828pp |7.902pp |0.1110 |93.80% |
| Prior-cycle signed correction |5.875pp |7.944pp |0.1118 |93.32% |

House contributes484 cases: MAE6.302 baseline versus6.342 historical. Senate contributes145:4.246 versus4.319. The average90% interval width is27.49points overall. Wide intervals and selection of polled races limit what coverage means. Races within a cycle are dependent;629cases do not equal629independent environments. There are only five held-out midterm cycles. The small performance difference does not establish that a correction is always harmful, but supplies no reason to promote it as the2026 default. Reversal is not scored as though it were trained.

## Run and update

From the repository's development environment:

```sh
uv sync --locked --extra dev
uv run --extra dev lab call election_status --profile election-research --arguments '{}'
uv run --extra dev lab call election_forecast --profile election-research \
  --arguments '{"dataset_id":"us-2026-11-01","config":{"as_of":"2026-11-01"}}'
uv run --extra dev lab call election_backtest --profile election-research --arguments '{}'
uv run --extra dev python scripts/election_research_report.py \
  --include-provider --output-dir /tmp/election-research-2026-11-01-v0.1.1
```

`election_forecast` accepts at most ten races per call and returns bounded margins, uncertainty, scenario probabilities, poll ages, calibration coverage, config and an input SHA256. An empty inline request returns `missing_data`. Caller-supplied inputs are labeled unverified; `synthetic=true` labels software fixtures and never selects a real bundle. `election_status`, `election_forecast`, and `election_backtest` are the same typed operations over CLI and stdio MCP.

To add observations without editing model code, create a `ForecastRequest` JSON file from the exported schema (`lab contracts --profile election-research`). Set explicit race identities, election/map vintage, `voting_system` and `poll_round`, D/R shares, field dates, availability basis, source IDs/URLs and verified-result n status. Unknown or unsupported eligibility yields no probability; known jurisdiction rules override a mistaken plurality label. Use publisher release dates when known; provider-added dates must retain their proxy label. Record party evidence, source flags and rejected rows separately. Do not count two versions of one survey independently. Inspect and save the source evidence before running:

```sh
uv run --extra dev lab call election_forecast --profile election-research \
  --arguments-file /absolute/path/to/reviewed-forecast-input.json
uv run --extra dev python scripts/election_research_report.py \
  --forecast-input /absolute/path/to/reviewed-forecast-input.json \
  --output-dir /tmp/election-research-next-snapshot
```

Use a new output directory for changed inputs; report scripts refuse to overwrite different evidence. Historical normalization is reproducible offline from pinned bytes:

```sh
uv run --extra dev python scripts/election_import_history.py \
  --source data/elections/sources/538-raw-polls.csv
```

The package depends only on Pydantic and the Python standard library. Domain services import no toolkit host, game, MCP, or web framework. The host adds only reviewed lazy registration and an explicit profile. Routine operations fetch no URLs and accept no filesystem paths, credentials or authority parameters. Host startup creates its ordinary managed runtime state; terminal report/import scripts write only explicitly chosen output artifacts. No game/native evidence gate is changed.

## Broader opt-in polling feed

A separate `votehub-2026-11-01` dataset expands usable sensitivity research beyond the four primary releases. The locally retrieved [VoteHub API](https://api.votehub.com/polls?from_date=2026-10-01&to_date=2026-11-01) snapshot contains **302 records**, including113Senate and65House questions. Conservative QA retains **22 polls across17 races and15 firms**;280 records are retained in the rejection audit. Feed contents changed from an earlier independent301-record retrieval, so the local source hash and timestamp identify this exact snapshot.

[VoteHub documents CC-BY4.0 and end-date filtering](https://votehub.com/polls/api/). Raw feed bytes and a separate manifest are retained under `data/elections/sources/`. The adapter verifies exact answer labels against a preserved [FEC candidate-master snapshot](https://www.fec.gov/files/bulk-downloads/2026/cn26.zip), scoped to full name, office, state, district and2026 election year. FEC affiliation is a reported party, **not certification of ballot nomination**. No candidate's party is inferred from answer ordering, first name, geography, or model output. Unmatched names and unknown third-party choices block acceptance; there is no fuzzy automatic party join.

These22polls are **provider-sourced, candidate-party-verified research sensitivity inputs**. Their numerical values, likely-voter sample counts, modes and release dates have not each been checked against the original poll release. `created_at` remains a provider-added availability proxy. Every such poll has `sample_size_verified=false`; the engine uses its disclosed n=1000 numerical placeholder. Source URLs, reported sample/population, sponsors, internal/partisan flags and individual rejection reasons remain available in the versioned JSON audit. Primary-release rows take precedence over matching provider survey windows. Conservative import excludes flagged partisan/internal polls, ambiguous survey variants, unsupported populations and Alaska ranked-choice contests. A first-choice D/R margin is not a ranked-choice final-win probability.

House geography follows the2026 poll subject and the exact candidate/office/district join; boundary products have not been independently reconciled. No historical district baseline or cross-map extrapolation is used. The strict accepted subset itself is selected and incomplete; it does not repair whole-chamber coverage or validate its probabilities.

Inspect `election_status.aggregator_race_ids`, then select at most ten IDs per call. A request that would exceed the bound fails explicitly rather than silently dropping races:

```sh
uv run --extra dev lab call election_forecast --profile election-research \
  --arguments '{"dataset_id":"votehub-2026-11-01","race_ids":["2026-NC-SEN","2026-OH-SEN"],"config":{"as_of":"2026-11-01"}}'
uv run --extra dev python scripts/election_verify_candidate_evidence.py --check
uv run --extra dev python scripts/election_import_votehub.py
```

The offline import command now invokes the raw-FEC exact mapping verifier before processing polls or creating any output. `votehub-v2.json` retains that verification proof and all source hashes. Altered parties, chambers and districts fail closed. The importer also verifies the retained feed hash and primary-survey overlap exclusions, and refuses different existing evidence. Changed source bytes or a different review policy require a new explicit output path. The model accepts reviewed inline inputs through `--arguments-file`; neither importer nor computation needs a model-code change to evaluate additional observations.


## Saved outputs and interface contract

The completed local run is in [`data/elections/reports/2026-11-01-v0.1.1/`](../../data/elections/reports/2026-11-01-v0.1.1/): primary race scenarios, symmetric Republican stress, two bounded provider batches with their exact request files, historical evaluation, research status and a content-hash manifest. Two complete report runs reproduced identical bytes. These files contain real source-based research outputs; synthetic fixtures are confined to tests.

For example, the primary Kansas baseline is D+4.0, with a90% scenario interval from−7.0 to+14.9 and probability of a positive D−R margin about73%. Texas is D+2.0, interval−8.9 to+12.8, probability about62%. Both are November1,2026 research scenarios with wide historical-error uncertainty, derived from one retained firm per race. They do not establish a poll miss or a voter-switching mechanism. The opt-in provider Ohio example is D+4.23, interval−6.83 to+15.57, probability about74%; its extra source/sample/date caveats remain attached. Probabilities are approximate and not independently calibrated2026 forecasts.

The concrete dashboard-facing schema is exported in [`schemas/lab-v1.json`](../../schemas/lab-v1.json) and generated [`docs/tool-contracts.md`](../tool-contracts.md). The independent Python API is `ElectionService.status()`, `.forecast(ForecastRequest)` and `.backtest(BacktestRequest)`. `ForecastReport` includes model version, resolved-input SHA256, configuration, data origin, as-of date, partial/missing status, recommended scenario, race coverage, poll ages, empirical calibration and per-scenario D−R margins/probabilities/quantiles. Transports wrap it in the standard toolkit result envelope. No dashboard compatibility beyond this explicit versioned contract is assumed.

QA rejects most provider records because candidate labels cannot be matched exactly or do not yield a unique verified D/R pair; it also excludes unsupported offices, partisan/internal records, duplicate versions, existing primary surveys, impossible dates/shares and ranked-choice contests. Rejection reasons overlap and must not be summed as independent counts. The saved audit preserves every record's disposition. The read-only FEC verifier reconstructs all188 subject/answer joins from the original ZIP, verifies108 retained mappings including92 usable D/R labels, and leaves80 unmatched.

See [verification evidence](verification-v0.1.1.json) for exact commands, test outcomes and the execution-boundary difference observed in the initial native-process regression run. Game observations and native Windows/game deployment were not run or claimed.


## Voting-round validity correction (0.1.1)

A review found that an earlier normalization could compare first-choice polls with ranked-choice outcomes, and inline inputs had no explicit voting-round gate. Version0.1.1 corrects both paths. `Race` and `HistoricalRace` require explicit supported eligibility to be modeled: `voting_system` is `plurality` or `two_candidate_runoff`, and `poll_round` is `decisive`. Defaults are unknown and produce no probabilities. Ranked-choice, majority-runoff, first-choice and unknown cases remain unsupported. Known state/year rules also block misleading plurality labels; this is a conservative scope boundary, not a runoff or transfer model.

Historical v3 excludes Maine federal contests from2018 onward, Alaska from2022 onward, Georgia federal November contests, and ambiguous Mississippi2018Senate contests. Louisiana November archive rounds are conservatively excluded pending historical regime/round review; none appeared in the selected source subset. These rules deliberately discard some potentially usable observations rather than assume comparable rounds. The active history shrinks from771 to749 snapshots; evaluated cases shrink from647 to629. Raw source bytes, earlier normalized datasets, manifests and0.1.0 reports remain unchanged for audit. Use the new0.1.1 report directory and active API outputs.

Primary evidence includes the [Maine2018 congressional RCV court record](https://legislature.maine.gov/doc/2493), [Alaska's official2022 implementation announcement](https://www.elections.alaska.gov/2021/03/), [Georgia's official general-runoff discussion](https://sos.ga.gov/news/secretary-raffensperger-calls-general-assembly-put-georgia-voters-santas-nice-list-and-end), [Louisiana election types](https://www.sos.la.gov/elections-voting/types-of-elections), and [Mississippi2018 official runoff results](https://www.sos.ms.gov/elections/electionresults_aspx/elections_results_2018_countyrunoff.aspx). Some direct official-page downloads returned403/502; search-index evidence and the accessible official court record support the conservative exclusions, and no inaccessible document bytes or document hashes were invented.

The legacy output field `dem_win_probability` is explicitly a positive D−R-margin probability in a supported decisive comparison, not a model of independent third-party wins. The schema description and `ForecastReport.probability_target` make that interpretation machine-readable. No RCV final-win probability is produced.
