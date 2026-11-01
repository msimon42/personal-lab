# Aggregate election research operations

The [research module documentation](../elections/README.md) records model
assumptions, dataset coverage, provenance and evaluation limits.

The `election-research` startup profile exposes `lab_*` and `election_*`
operations. It does not load the game domains. The optional toolkit extra is
`personal-lab-toolkit[election-model]`; this repository's development environment
includes it. No frontend, model API, credentials, network access or source-root
configuration is required to execute these operations.

Start with coverage and provenance:

```sh
uv run --extra dev lab call election_status --profile election-research --arguments '{}'
uv run --extra dev lab contracts --profile election-research
```

Select the dated public snapshot explicitly to calculate research scenarios for
its four covered races. This is partial election coverage and cannot support
chamber-control probabilities:

```sh
uv run --extra dev lab call election_forecast --profile election-research \
  --arguments '{"dataset_id":"us-2026-11-01","config":{"as_of":"2026-11-01"}}'
```

The same status response advertises an opt-in provider dataset:22 polls across17
races/15 firms after strict candidate-party and survey QA. Its numerical values,
publication proxies and sample sizes retain provider caveats. Select up to ten
IDs from `aggregator_race_ids`; no races are silently dropped:

```sh
uv run --extra dev lab call election_forecast --profile election-research \
  --arguments '{"dataset_id":"votehub-2026-11-01","race_ids":["2026-NC-SEN","2026-OH-SEN"],"config":{"as_of":"2026-11-01"}}'
```

An empty inline-input request returns an explicit missing-data result. It does
not silently select a current dataset or invent observations:

```sh
uv run --extra dev lab call election_forecast --profile election-research \
  --arguments '{"config":{"as_of":"2026-11-01"}}'
uv run --extra dev lab call election_backtest --profile election-research --arguments '{}'
```

The CLI `call` command and MCP use the same typed application operations. Select
a separate managed state directory with `--state-dir` when needed. The MCP host
starts with:

```sh
uv run --extra dev lab mcp --profile election-research
```

For a saved reproducible input object, replace `--arguments JSON` with
`--arguments-file PATH`. The terminal command reads at most 262144 bytes of UTF-8
JSON and applies the same operation schema. File paths are not MCP arguments.

An agent should inspect `election_status` and the concrete input schemas first,
then supply up to ten aggregate races and their polls to `election_forecast` when
actual usable observations are available. Each race must explicitly declare a supported voting system and decisive poll round.
Unknown or unsupported eligibility produces no probabilities, and known jurisdiction
rules override misleading labels. Every supplied poll must include its field
dates, availability date, source ID and population/mode metadata. House races
require a map vintage. Use the cutoff date applicable to the analysis, and mark
synthetic fixtures with `synthetic=true`. Do not describe fixture output as a
real election forecast.

Compare the baseline, estimated historical error and hypothetical reversal
outputs together. A hypothetical positive Democratic adjustment is a sensitivity
case; it is not evidence that a hidden voter shift exists. Inspect omitted races,
calibration coverage and warnings before summarizing a result. The operations
provide no voter-level inference, demographic political profiling, campaign
targeting, or full-chamber seat probabilities.

All election operations are read-only observations: local calculations over
packaged public evidence or bounded supplied inputs. The host may initialize its
ordinary managed runtime state when starting. Tool arguments cannot change the
startup profile, import a toolset, read arbitrary paths, fetch URLs, or authorize
an external effect.
