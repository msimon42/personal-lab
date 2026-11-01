# Offline verification and separate qualification

Use Python 3.12.16 with the committed workspace lock:

```sh
uv sync --locked --all-packages --extra dev
uv run --extra dev python scripts/check.py
```

The check script runs Ruff, public-contract type checks, generated-contract drift
checks, all pytest tests and all 20 executable synthetic fixture families. It needs
no browser, Node, Steam, game, VM or model API key. All writes use disposable test
roots. Domain checks preserve bytes, repeated occurrences, semantic unknowns,
complete obligations, source/update invalidation and recovery.

Headless tests cover direct application operations, actual MCP subprocesses,
concrete schemas, native confirmation on legacy/modern protocol paths, explicit
paging, source exclusions, revision/idempotency races, bounded queue acceptance,
crash states, exact artifacts, installed wheel/sdist closure, independent core-only
installation, and backed-up historical-data relocation.

The six former browser-domain workflows are now covered by physical fixtures and
headless workflows: exact repair ZIPs, explicit conflicting intentions, malformed
input refusal, unknown obligations, stale-source refusal and CK3 read-only
selected-playset investigation. Browser navigation, origins, SSE and frontend
build tests retired with their product shell.

`docs/verification/codex-host-smoke.json` records a separate actual Codex host
capability call. That live-host usability check is not a required CI/model call.
SDK transport and workflow tests remain deterministic and offline.

CI runs the offline suite on macOS and Windows. Native Windows locks/paths,
disposable game deployment and actual-game conformance require independently
observed evidence. A configured CI job is not an executed local Windows result.
Recorded logs, skipped runtime tests and synthetic results cannot claim game
success. No game launch is part of the default check script.
