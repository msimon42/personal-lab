# Personal Lab architecture delivery snapshot — 1 November 2026

This record captures the completed architecture migration and local cutover before subsequent toolsets and publication. Its test counts and release fingerprint describe that verified checkpoint.

The headless foundation is implemented. The existing EU5/CK3 engine remains the first toolset; no additional daily-life product was introduced.

- Workspace packages: retained `eu5-mod-engine`/`backend`, extracted `toolkit-core`, and `personal-lab-toolkit` version 1.0.0. Python 3.12.16 and MCP SDK 2.3.0 are pinned with the shared lockfile.
- Unified `personal_lab` stdio host exposes **54 concrete tools**: 5 core, 17 EU5 and 32 supported CK3 operations. The former HTTP hop, web frontend, browser build/CI and obsolete launchers are removed. Unsupported game-test/activation operations remain unregistered.
- Both transports and CLI share strict application contracts, bounded outputs/artifacts, persistent bounded runs, revision/idempotency checks, policy-aware source disclosure, exact native questions, terminal fallback and immutable external-action grants. Restore now creates a distinct instance and invalidates prior authority while retaining investigations.
- Actual Codex CLI 0.164.0-alpha.11.1 called `lab_capabilities` once against disposable core-only state and exited successfully. SDK tests cover actual stdio workflows, legacy/modern native-choice paths, malicious/stale inputs, installed wheels/sdists and domain-free core-only installation. See `verification/` for the sanitized client evidence.

## Checks actually executed

From the renamed checkout `/Users/matthewsimon/projects/personal-lab`:

`UV_CACHE_DIR=/private/tmp/eu5-uv-cache /private/tmp/eu5-ck3-tooling/bin/uv run --extra dev python scripts/check.py`

**839 pytest tests passed in 72.89 seconds**, Ruff passed, public-contract mypy checks passed, generated schemas/docs matched, and **all 20 synthetic fixture contracts passed**. Source checks `git diff --check` and `git diff --exit-code -- specs schemas/ck3` passed. The old checkout's baseline was 689 backend tests plus 14 frontend unit tests; relevant browser/domain assertions now have headless coverage.

Matching wheels and source distributions for all three packages were built and tested. Build output excludes deleted transports, including when an old build directory contains stale files. The installed environment is outside the checkout and uses the exact exported runtime constraints. At cutover, source, installed modules and workflow receipt matched fingerprint `3038575d76148be6a900eb60de418d8c7c49a5e9c5c0dba63b34ae9465175c5b` (see live receipt for the authoritative value).

## Live cutover and renames

The verified idle legacy engine PID 23984 was gracefully stopped after exact HTTP/native identity and idle-state checks. Its two approved libraries were completely backed up, migrated and verified under `~/Library/Application Support/personal-lab/instances/`. Retired original copies and complete backups are retained outside the checkout.

- `personal`: 57,042 immutable files preserved; the original 80 records were unchanged by relocation. Fresh stdio resumed the same CK3 workspace with **5 scans, 2 indexes and 29 jobs**, without a new scan or game launch. One new briefing and instance configuration are expected operational metadata.
- `ck3-isolation-qualification`: immutable plan/policy/review files preserved; its single unlaunched probe is historical and execution-ineligible. Fresh preparation and approval are required before any future qualification.
- `eu5-legacy`: separately authorized on 1 November; 1 workspace, 2 scans, 8 completed jobs and all saved events preserved. Its artifact link was mapped from the absent former checkout root using verified backup membership; 2 candidates require revalidation. All 8 immutable files and the original database were verified unchanged.
- `ck3-test-qualification`: separately authorized on 1 November; all 5 immutable files preserved and its single prepared/unlaunched probe made historical and execution-ineligible.

The additional libraries have separate complete backups and unchanged retired originals under `backups/pre-monorepo-20261101/` and `retired-sources/pre-monorepo-20261101/` in the same OS-local Personal Lab directory. The nested CK3 library was migrated and archived before the remaining EU5 root. The checkout now contains no `.workspace` state. See `verification/additional-library-relocation.json`; full local receipts are under `migration-reports/2026-11-01/` outside Git.

The owned connector/skill/receipt now use `personal_lab` and `ck3_` names, preserving unrelated settings and existing stricter policies. Checkout renamed to `personal-lab`; GitHub renamed to `msimon42/personal-lab` with the same repository ID 1436266773 and updated origin. The existing project policy was moved to the new checkout path without changing its values. The renamed folder was reopened through the installed Codex CLI and registered as the Personal Lab project. This existing chat still references the old project and loaded tool definitions; use the renamed project and reconnect tools to load the new interface.

The migration was verified locally before publication. Git history and the pre-migration dirty-tree checkpoint were retained.

## Remaining boundaries

Native Windows deployment/game qualification and real-game repair/activation/live undo remain unavailable. No game was launched and no conformed game rules were added. The original specification package and advisory schema source bytes remain unchanged.


---

# Historical implementation ledger

The following entries document earlier increments and their contemporaneous coverage. The accepted headless amendment supersedes historical UI and HTTP product requirements.

# Implementation status

30 October 2026. This is a working local implementation with an executable EU5 synthetic workflow and CK3 playset inspection. The complete research plan also includes Windows/game qualification that cannot be completed on this macOS host. **No EU5/CK3 conformance or universal compatibility is claimed.**

## Complete contributor retrieval and protected discussion, 30 October

Assistant version **3.1.0** adds `get_definition_contributions`, the connector's
34th tool. Previously a symbol could report more than eight contributors while
exposing only its first eight, with no declaration continuation; an owner filter
could return other owners' declarations. Symbol/related previews now provide
complete next-request arguments, and the new tool pages every matching indexed
occurrence within 50 records and 128 KiB. Target, filters, offset/limit, saved
identity and policy are bound into continuations; changed inputs require a fresh
page. Counts describe captured metadata, not game-selected winners.

Same-file script and localization repeats retain their exact captured coordinates.
Deferred localization reuses hash-bound index coordinates without rereading
originals. Missing coordinates remain explicit provenance-only gaps. Projection
format 2 retains earlier artifacts and rebuilds from saved scans without a new
source scan.

Saved goals, notes and next steps now carry separate policy, value and source
bindings. Public context, session, remember and resume responses withhold prose
whose policy/scope no longer matches while retaining the managed original.
Goal-only updates cannot authorize old notes, and next-step-only updates cannot
relabel old notes as current. Concurrent progress/field saves retain unrelated
updates. Legacy unbound text is labeled and remains readable with no exclusions;
it is withheld when exclusions exist. Excluded saved choices produce a generic
fresh-review marker so other resumable progress remains available.

Validation: **689 backend tests passed in one full run**, including 34 new
contribution cases, 31 saved-context cases and two MCP-to-authenticated-HTTP
integration cases. Installed direct-wheel and source-distribution smoke tests
exercise the new tool. Ruff, generated contracts, skill validation and original
specification/schema preservation checks passed. The existing Starlette warning
remains. No frontend changes/checks or native Windows/game qualification occurred.
The separate isolation launch remains pending explicit user approval.

Installed the owned 3.1 update and restarted only the verified idle engine.
Current source, receipt, engine and fresh connector match. A saved real-installation
symbol preview showed eight of nine captured declarations; the new tool returned
the remaining declaration with nine unique identities across both pages. Owner
filters returned only the selected owner's declaration. Public context/session
disclosure metadata was checked without logging saved prose. The library retained
5 scans, 2 indexes and 29 jobs, with no new scan/check job. Projection format 2
was prepared while retaining the earlier projection. Existing settings/policies
were preserved; the new read-only tool received its narrow default policy.
Managed reports are under
`.workspace/ck3-verification/reports/owned-connection-update-3.1-*`.
The host's already-loaded connector still requires reconnection.

## Installed update and prepared isolation check, 30 October

Updated the owned Codex installation through its recoverable installer and
gracefully restarted only its verified idle headless engine. Current source,
installed receipt, fresh stdio connector and loaded engine now match. Fresh
context/resume calls exposed all 33 tools and retained the same investigation.
The library kept its 5 scans, 2 indexes and 29 jobs; no scan/check job was added.
Unrelated settings and every existing per-tool policy were preserved. The current
host's already-loaded connector still requires reconnection. Managed audit
reports are under `.workspace/ck3-verification/reports/owned-connection-update-*`.

Prepared a fresh immutable Mac isolation plan against the identified CK3
1.19.0.4 installation in `.workspace/ck3-isolation-qualification`. Its private
profile requests no mods; actual loaded content remains unobserved. Candidate
argument strings were found in the executable, which does not qualify their
behavior. The native disposable-Python write control passed: a private-profile
write succeeded and an outside write was denied. Launch preconditions still
matched afterward, including the normal-data metadata inventory. No game was
launched, no normal playset was edited and no gameplay capability was enabled.

The reviewed next action is a bounded 90-second operator isolation launch.
Computer-use inventory reports the Mac locked, preventing direct window
observation. Manual unlock and explicit consent for that concrete launch remain
required. The plan, control result and launch review are archived in the private
managed qualification root; they establish preparation, not game conformance.

## Agent consistency and isolation lifecycle, 30 October

The assistant's release fingerprint now covers every backend Python module and
the exact advisory schema/manifest bytes. Core-only updates therefore block
mixed-release preparation, native approval prompts and writes. Saved inspection
reports remain readable for core-only changes when their own scan/schema scope
is still valid. Installed direct-wheel and source-distribution smoke tests verify
the same release fingerprint as the source checkout.

Legacy list/excerpt tools now enforce the current source policy through return,
redact complete comparisons with an excluded contribution, and preserve explicit
excluded-group accounting. Excluded names/targets are removed before searching.
Changed policy/scope/revisions refuse stale content and new preparations; pending
choices and file writes are blocked after exclusion. Signed native undo still
retires an inactive artifact without disclosing excluded source labels, while
retaining its exact recovery files and intervening-edit checks.

Operator-only Mac isolation probes now bind a versioned launch plan, arguments,
executable, policy and exact empty-mod profile. Source separation and inventory
errors are checked before profile creation. Launch intent is durable and
serialized across SQLite connections; ambiguous interrupted launches cannot be
retried automatically. Status observes microsecond process creation identity,
zombies and process-group descendants. Stop remains pending until observed exit;
uncertain ownership never authorizes a signal. These observations do not establish
game initialization or byte preservation of the normal data directory.

Validation: **622 backend tests passed in one full run**, including 20 isolation
setup/lifecycle cases, 28 new legacy-policy regressions, core/schema-only release
regressions and installed-package MCP checks. Native controls used disposable
Python processes, including ignored termination and surviving descendants. Ruff,
generated contracts and original-specification/schema preservation checks passed.
The existing Starlette test-client deprecation warning remains. No frontend code
changed or frontend checks ran in this increment. No CK3 game launch, native
Windows qualification, activation or gameplay conformance was performed.

Existing installations must reinstall their owned workflow and restart/reconnect
to load the expanded release fingerprint. Legacy unbound isolation plans require
fresh preparation; existing scans and inactive recovery artifacts are retained.

## Installed headless assistant increment, 30 October

The headless CK3 assistant now runs from an installed wheel independently of the
application source checkout. The build copies the original advisory schemas into
package resources without changing their bytes or hashes. Source distributions
retain those originals and the build hook. The source-and-built-UI ZIP now also
includes the agent skill, references and build inputs.

Two new integration cases build a direct wheel and a wheel from a source
distribution, install each in a temporary environment, and exercise the installed
CLI, packaged knowledge/workflow, physical synthetic-source scan, investigation
index, and actual stdio MCP source reads/report. Both passed on macOS. All 24
schemas and their manifest match the original bytes; all four synthetic source
files remain unchanged per case. Temporary headless engines are stopped after
the checks. Game-test and activation tools retain their unavailable results.

All **569 existing backend tests** passed across the main run and targeted
process-permission reruns; the **two new installed-package tests** also passed.
The initial restricted run passed 567 cases but could not bind localhost or run
the native Mac isolation control; the affected tests passed with those process
permissions. Ruff and generated-contract checks passed. Frontend code was not
changed or retested for this packaging increment.

The wheel, source distribution and source-and-built-UI ZIP were built locally;
the wheel's 25 schema resource files match the originals, and the ZIP's 172 file
hashes verified. This qualifies the tested headless package path on macOS with
locked dependencies. The wheel excludes the optional dashboard. Native Windows
delivery, CK3 gameplay testing, activation and live undo remain unfinished.
Earlier package limitations below describe historical increments before this fix.

## Complete investigation increment, 30 October

Release **3.0.0** is installed in the existing Codex connection with **33 tools**.
It adds player-oriented concern groups, bounded evidence packages and batch
excerpts, structured review records, reports and continuation, automatic searches
of known local mod locations, separate player-selected candidate inspections,
and saved campaign-version preferences. Existing tools and saved notes remain
available; historical reviewed IDs do not count as evidence-backed completion.

Every raw finding belongs to a concern. Reviews preserve source references,
interpretation, uncertainty, remaining questions and revisions. All completed
dispositions require citations covering readable differing contributions;
missing-source/unsupported blockers require actual engine-observed gaps. Source
policy changes redact affected saved explanations. Reports distinguish finished
accounting from unresolved questions and untested game behavior. Captured missing
mods page independently from current launcher metadata.

Investigation projections and small revision-keyed headers are reused without
repeated full-scan JSON decoding. Cache memory is bounded to 256 MiB combined;
source bytes are still verified per response, and scope/policy are rechecked
before returning results. The final synthetic 58,000-entry lookup benchmark
returned evidence packages at **0.040 seconds p95**, with zero warm projection
rebuilds. This corpus tests lookup metadata scale, not scanning a complete game.

Twenty bounded evidence requests against the saved actual **mods** scan returned
in **0.065 seconds p95** (29 KiB package). The earlier same saved-source sequence
took roughly 1.59 seconds before the header optimization. This measures one
concern and is not a whole-library performance guarantee. The engine retained
the existing scan: 2,641 raw observations grouped into 159 concerns. Those numbers
are neither confirmed bugs nor newly completed reviews.

Validation: **569 backend tests passed**, Ruff and generated contracts passed;
**14 frontend tests** and production build passed; the skill validator passed.
Native local-engine and Mac process-boundary tests used disposable data. No
native Windows execution or CK3 gameplay qualification occurred. Performance
artifacts are retained under
`.workspace/ck3-verification/reports/assistant-v3-evaluation/`.

Three actual fresh Codex conversations against 14 disposable synthetic source
files exercised check → interrupted review → resume → chosen alternative.
All detected concern groups were accounted for, resume created no new job, and
the candidate produced a separate inspection while retaining the campaign
preference. All source bytes remained unchanged; no proposals or approvals were
created. The first check completed without tool errors; subsequent conversations
recovered from invalid bounded arguments and a rejected cross-workspace citation.
The exposed tool limits were tightened in response. Transcripts and summaries
are retained with the performance artifacts. These conversations and deterministic
regressions are not CK3 gameplay observations.

The accepted behavior and bounds are recorded in
[decision 0005](decisions/0005-complete-ck3-investigations.md).

## Context and knowledge increment, 30 October

Release 2 of the CK3 assistant is installed into the existing Codex connection.
Its **25 tools** now provide a fast current-playset briefing, a coordinated check,
saved conversation context, bounded source searches, related definitions and
investigation briefs. A new check follows the launcher's current selection;
“continue” resumes the saved investigation. Distinct installations remain
separate, old notes remain historical, and missing-mod previews can be paged.
The briefing does not hash the whole installation; source verification runs only
when a check is requested.

The authored knowledge library contains **24 entries and 26 fingerprinted
sources**, covering CK3 scripting, loading, content families, preservation, game
limitations, application limitations and unresolved behavior. It includes
references to installed 1.19.0.4 documentation and pinned Tiger/CWTools sources.
Public research is allowed automatically when needed, using public concepts
without sending private file contents or local paths. Documentation and saved
notes cannot create runtime evidence, repair authority or approval.

The installer updates instructions, references and connection settings through
a recoverable generation. It preserves unrelated settings, comments and explicit
permission choices. Nineteen inspection/housekeeping tools receive narrow default
permissions; actual change tools retain their existing policy. Loaded processes
retain their release fingerprints, and mismatches block preparation and approval,
including updates while a native choice form is open. Older prepared artifacts
remain readable and can be retired without enabling obsolete choices.

The updated headless engine retained the existing verification library. Its
current-playset briefing returned in **0.474 seconds** on this Mac; a bounded
call-allies brief over the saved actual-installation scan returned in **3.252
seconds**. These are individual observations, not performance guarantees or a
new full-installation scan. The active collection still has 100 unavailable
enabled payloads. No original game/mod files were changed and no game was run.

Two fresh actual Codex conversations passed against a disposable physical CK3
layout, with no supplied internal IDs or conversation history. The first checked
the selected playset, read captured scripts and launcher descriptors, consulted
the knowledge library and saved its findings. The second resumed those findings,
stated their age and created **no additional scan/check job**. Both completed with
zero failed tool calls, preserved all nine source files byte-for-byte and created
no proposals or active-state changes. Transcripts, answers, counters and hashes
are archived in ignored managed reports. Earlier runs exposed and resolved host
approval friction, an empty path-query contract mismatch, inaccessible captured
descriptors and unnecessary rechecking on resume. Existing Codex clients must
reconnect after the update to load the new registered release settings; both
fresh clients reported a matching ready connection.

Final backend checks: **507 tests passed**, Ruff passed, and generated contracts
matched. Frontend checks for this increment passed all **14 tests** and the
production build. The installed skill passed validation. Resource packaging was
checked in an isolated wheel installation; full standalone engine delivery is
not qualified because existing schema/profile loading still uses the checkout.
Native Windows execution and real CK3 testing/activation remain unrun.

See [agent operations](operations/AGENT.md) and the
[accepted context/knowledge decision](decisions/0004-agent-context-and-knowledge.md).

## Codex agent increment, 30 October

The CK3 conversation workflow and named local MCP connection are installed on this
Mac. The idle CK3 dashboard process was replaced with a headless engine using the
same managed verification library; the separate unrelated app was left running.
The first release's actual stdio MCP calls exposed 15 bounded tools. The
engine is loopback-only, retains one coordinator, and mounts no frontend.

The agent index was also built successfully from the active playset's existing
immutable scan, with live source checks before and after. It contains 2,683
groups, including 119 differing cross-mod groups outside metadata and 218
differing within-mod groups; 100 enabled payloads remain unavailable. These are
file observations, not a count of broken features. A read-only tool query
confirmed the call-allies comparison against the captured base game.

Delivered complete searchable investigation indexing, bounded source inspection,
native user choices tied to exact proposal content, inactive call-allies fixes
mod preparation, stale/tampered/cross-workspace rejection, saved progress, and
retirement of inactive changes with recovery copies. Missing mods and unknown
game behavior remain explicit. [Agent operations](operations/AGENT.md) describe
setup, coverage and limitations; [the interface decision](decisions/0003-codex-agent-interface.md)
records the user's accepted change outside the preserved specification package.

Checks actually run on this Mac: **426 backend tests passed**, Ruff passed,
generated contracts matched, **14 frontend tests passed**, and the frontend
production build passed. Backend tests include a real temporary headless engine
shared by two connectors, actual stdio MCP calls, native choice-form replay, and
a native Mac process-boundary control using disposable files. These are synthetic
software/OS checks, not CK3 gameplay evidence. One existing Starlette/httpx
deprecation warning remains. The system Node 18 was too old; frontend checks
passed with the installed Node 22.

The first complete fix/test/enable/undo release remains **unfinished**. Test tools
return `not_run` and activation refuses. The initial actual-source comparison
found Unofficial Patch's call-allies action identical to the captured base game
after comments/formatting are excluded; More Interactive Vassals adds behavior.
A separate real game test must establish whether those additions are lost.
An empty private Mac isolation probe was prepared without launching CK3. Native
Windows execution, CK3 launch/feature observations, live activation and live undo
have not been performed. No original game, mod, Workshop or normal playset files
were changed by this increment.

## CK3 inspection increment

Delivered game/platform-scoped workspaces, migration of legacy workspaces to
EU5/Windows, CK3 Steam/local/Workshop discovery, consistent launcher DB/WAL capture,
playset selection/import and source staleness checks. CK3 parsing and advisory
analysis preserve all source contributions without selecting loader winners.
The original 24 schema files remain unchanged; a hashed overlay manifest records
corrections and inspected documentation provenance. The UI supports the complete
discovery/import/scan/source-investigation flow and keeps repair/activation gated.

CK3 has separate proposed experiments and game/platform evidence checks. The
inactive writer assembles exact generated bytes plus fresh CK3 descriptors; no
automatic CK3 transformation or game execution is enabled. Read the [CK3 coverage
and workflow](operations/CK3.md) and [accepted decision](decisions/0002-ck3-mac-first.md).

The checks below were rerun on macOS for this increment. CK3's Windows cases use
synthetic Windows-target discovery contracts on this host; native Windows APIs,
game performance, game loading/behavior and runtime conformance remain unrun.

## Local CK3 application exercise, 30 October

Ran the app against the user's authorized Steam installation and launcher state
on macOS, preserving originals read-only in a separate managed verification root.
All six saved playsets were discovered and imported. Available-source scans
completed for all five populated playsets; the empty selection received metadata
inspection only. Fresh discovery after the scans matched all six imported
configurations. Five populated selections have unavailable enabled payloads;
the active Immersion v1.14 selection has 27
available and 100 unavailable out of 127 enabled entries. Launcher database order
agrees with `dlc_load.json` for that active selection. This checks serialized
state and available sources, not the content loaded by CK3.

The active scan captured 59,088 files (23,100,458,971 bytes), with 2,903 files
inside the 120 MB text inspection budget. Deferred text and unsupported assets
remain immutable opaque bytes. Inspection found 38 paths shared by multiple
mods; overlaps and duplicate identities are review candidates, not established
incompatibilities. No game was launched, playset activated, or repair promoted.
Private reports and captured content stay in ignored managed state.

This full-installation exercise exposed and corrected text-budget starvation,
redundant blob copies, oversized recursively duplicated derived syntax, excessive
progress persistence, overview counters that used loaded-page lengths, and
generic missing-payload labels. Historical records retain their original IDs;
display labels recover the exact captured diagnostic provenance when it matches.
Complete source hashes, immutable bytes, definition identities and preservation
obligations remain authoritative; bounded displays identify their partial scope.

## Working application

- Local FastAPI service, React/TypeScript workbench, compact transactional SQLite state, installation session capability, origin/host checks, strict command payloads and actual request-body budgets.
- Immutable blobs, stable source inventory, streamed opaque assets, target Windows path policy, contained descriptor/native-handle reads, source/managed-root isolation and cross-process coordinator locks.
- Bounded lossless parser/CST, original serialization, malformed/encoding preservation, occurrence-aware edits, byte/code-point/UTF-16 positions, family front ends and bounded whole-document caching.
- Stage-aware file selection, explicit synthetic six-mode scheduling/readers, hidden-source provenance, typed graph projections, exact baseline-relative obligations, repeated/deleted contributions and symbolic unknowns.
- Deterministic synthetic repair staging, complete final-stack reanalysis, explicit design alternatives/waivers, current decision-set fingerprints, source/schema/parser invalidation and race-safe publication.
- Five UI screens, source/effective/staged views, snapshot history, jobs/cancellation/reconnection, bounded paginated detail reads, source previews, findings/obligation ledgers, independent gates and evidence inspection.
- Inactive ZIP review exports; journaled managed-release/rollback module with fault recovery and external-edit checks. The application does not expose unqualified game promotion.
- Complete runtime-state backup/restore with hash manifests, evidence/releases/journals/artifacts, no-overwrite restore, stale restored decisions/plans and required recovery for incomplete journals.
- Bounded OpenRouter executor, adversarial offline replay lane, model/provider/excerpt/budget settings and backend-only credentials. Model tools share local staging/validation boundaries; they cannot waive obligations or activate releases.
- Windows Steam/known-folder discovery, arbitrary-build byte fingerprinting, allowlisted runtime recipes, manual observation capture and raw log comparisons. Those adapters have offline contract tests, not executed Windows qualification.
- Windows 11 `Start-EU5.cmd`/PowerShell bootstrap with pinned checksum-verified local uv/Node, locked dependencies, UI build and browser launch. Source-and-built-UI ZIP packaging and a macOS/Windows CI definition.
- Physical synthetic fixture folders with immutable manifests, exact source hashes, safe materialization and disk-backed workspace scans. Changed, missing or additional source files invalidate the fixture contract.
- Playwright Chromium tests against the built UI and actual local backend, with isolated temporary databases, physical mod files, real HTTP requests, ZIP verification and retained failure diagnostics. One command prepares and runs this lane on macOS before VM deployment.

## Verification actually performed

| Check | Result |
|---|---|
| Python tests | **409 passed** on macOS/Python 3.12.16 after the local CK3 exercise fixes |
| Executable fixture runner | **F01–F20 all pass** their declared synthetic expectations, including blocked/no-write outcomes |
| React integration tests | **14 passed**, including existing EU5 workflows, CK3 creation/discovery/import/order/capability/evidence isolation, complete-summary/partial-detail display, and identifiable missing-payload findings |
| TypeScript and production frontend build | Pass on Node 22.22.0 |
| Python lint | Pass |
| Generated request types/OpenAPI drift | Pass |
| npm production dependency audit | 0 reported vulnerabilities at this check |
| Release ZIP | Built locally; dependency closure/hash-manifest regression covered |
| Windows bootstrap download checksums | Official uv x64/ARM64 and Node x64/ARM64 checksum URLs verified; script itself not run on Windows |
| Automated browser E2E | **6 passed** in the initial CK3 increment on macOS/Chromium headless: five existing EU5 workflows plus CK3 disk discovery/playset selection/import/advisory inspection, source preservation and capability gates. The CK3 case was rerun and passed after the local exercise fixes. The earlier five-test EU5 suite also passed headed |
| Manual browser verification | Local scan, provenance/source display, staging, independent validation and experimental export workflow exercised |
| Benchmark | Measured the small F01–F20 corpus only; no full-installation performance target or claim |
| Original specification package | Preserved; implementation does not change supplied specification/source assets |

The Python test run emits one FastAPI/Starlette test-client deprecation warning about its HTTP transport adapter. It does not affect passing results. No game log warning allowlist is inferred from this tooling warning.

The fourteen React tests use mocked API responses; the six Playwright tests run the actual application without API mocks. Browser tests preserve source hashes, leave active-playset/release files absent, and reject external network access. These are synthetic application contracts, not executed EU5/CK3 evidence. GitHub Actions is configured for macOS and Windows; this increment's Windows CI was not run from this session. The user's Windows 11 VM, launchers and actual game execution still require qualification.

## Qualification and remaining scope

| Plan area | Current boundary / required next evidence |
|---|---|
| M0 / W00 Windows 11 VM feasibility | Launcher and app must actually run in the selected VM; initialize the actual EU5 installation and archive baseline milestones. No VM is connected in this session |
| W01 discovery/playset/NTFS qualification | Native adapter code exists. Confirm known-folder/Steam paths, aliases/reparse behavior and actual active content/order on the selected Windows environment |
| W02 runtime driver qualification | Controlled recipes and observation capture exist. Qualify actual commands, cold starts, stage coverage, crash-driven disabling, direct sentinels and log grammar |
| W03 / E01–E25 conformance | **All experiments remain proposed/not-run. Zero C rules are shipped.** Measure each relevant build/stage/family/field before enabling its production capability |
| W04 game-directed repair/activation | Disabled. Requires current measured loading/readers, complete behavior obligations and a qualified activation method. Playset writers remain unavailable until persistence is conformed |
| Installed script-doc formats / language semantics | A versioned structured importer and opaque preservation path exist. Actual dump-format adapters, exhaustive type/scope/macros/runtime arithmetic semantics need the selected installation and qualification; unknowns stay explicit |
| Full managed-copy/playset repair strategies | Synthetic full-file composition is implemented. Real-game strategies and metadata/writer policies depend on measured family/activation behavior |
| Broader family production coverage | Metadata/localization/GUI syntax and synthetic composition are exercised; events/on-actions/setup/maps/assets remain outside automatic real-game composition until their own experiments pass |
| Live OpenRouter | Executor, policy UI and mock/replay contracts are implemented. No live provider request, key/spend, or endpoint evaluation was used during development |
| Release/deployment operations | Local package/backup/recovery checks pass. Windows installation/upgrade, actual filesystem interference and whole-stack runtime acceptance remain unrun |
| GitHub hosting | Source repository: [msimon42/eu5-validation-engine](https://github.com/msimon42/eu5-validation-engine). GitHub Actions is configured for macOS and Windows checks |

“Any EU5 version” means the app accepts and fingerprints arbitrary installations and keeps unsupported behavior visible. It does not mean one unmeasured loader assumption is applied to every release. Static analysis and experimental review can be useful before game conformance; validated game repairs remain gated.

## Run and continue

Use `uv run python scripts/test_e2e.py` for the complete synthetic browser lane on macOS, or add `--headed` to watch it. See [testing operations](operations/TESTING.md) for physical fixture imports, reports and individual checks. For a macOS preview, build the frontend and run `uv run eu5 serve`, then open [the local app](http://127.0.0.1:8765). The [Windows quick start](../README.md#windows-11-clone-and-launch) remains the VM entry point.

The next external qualification is W00 in the user's Windows 11 VM, then active-playset/driver qualification and a narrowly selected W03 campaign. The [implementation plan](IMPLEMENTATION_PLAN.md) remains the complete roadmap; this ledger distinguishes implemented software from experiments and production capabilities still requiring evidence.
