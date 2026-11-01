# CK3 through the unified agent interface

Start with “Check my current CK3 playset” or “Continue reviewing my mods.” The
`personal_lab` connection supplies `ck3_*` tools. `ck3_get_context` gives the
selected playset, missing-source preview, saved work, available actions and next
step; `selection="resume"` uses the previous investigation. Continue from saved
results unless a fresh check or write precondition requires current files.

Results use the shared envelope. Read `data`, limitations, source freshness and
all continuations. `lab_run_get` polls accepted work. Source files and prose
remain subject to current exclusions and provenance. Saved discussion is not
approval or game evidence.

## Setup and connection

From the checkout, `uv sync --locked --all-packages --extra dev` installs the
workspace. Fixture mode needs no game, browser, Node or model credentials.
Production installation uses matching wheels in a persistent environment outside
the checkout:

```sh
uv build --all-packages
uv export --all-packages --no-dev --no-hashes --no-emit-workspace --output-file dist/runtime-constraints.txt
uv tool install --python 3.12.16 --constraints dist/runtime-constraints.txt dist/personal_lab_toolkit-1.0.0-py3-none-any.whl --with dist/toolkit_core-0.1.0-py3-none-any.whl --with dist/eu5_mod_engine-0.1.0-py3-none-any.whl
lab install --profile personal --state-dir /absolute/local/personal-lab-state
```

The owned installer changes the connection and CK3 skill together, preserves
unrelated settings and stricter policies, and records exact release fingerprints.
Reconnect after an update. The new public names have `ck3_` prefixes; the old
connection and unprefixed names are retired.

MCP stdio directly owns services and one bounded worker. There is no detached
HTTP engine. One execution process may own a state directory; another process
fails clearly rather than opening a second writer. Tool calls inside the same
connection share execution. History survives exit, but an in-process job does not
continue after its owner exits. Interrupted work requires explicit resubmission.

Core, toolkit, mod code, knowledge, unchanged CK3 schema bytes and operating
instructions participate in release checks. Stale loaded code cannot prepare or
approve changes. Installing new files does not upgrade an existing loaded process.

## Investigation and choices

Use `ck3_list_concerns` and `ck3_get_concern_evidence` for the complete review queue.
Inspect bounded saved sources, related definitions and all declaration pages.
Compare each mod with the base game before describing feature loss. The knowledge
library retains sources and version limitations; public research never creates
executed conformance.

Save evidence-backed explanations with `ck3_record_concern_review` and check
completion with `ck3_get_investigation_report`. Notes supplement that tracker.
Unread work remains pending; a complete file review is not tested gameplay.
Missing payloads remain missing. Candidate copies are separate alternatives and
never silently become launcher-active sources.

Prepare concrete supported options before `ck3_ask_about_changes`. Verified native
responses bind to the exact pending question, wording, choices and proposal. A
host without native support returns `lab decide <question-id>` for an actual
operator terminal response. Model booleans, notes and silence cannot approve a
choice. Native/terminal design decisions remain separate from external-action
grants issued by `lab approve <plan-id>`.

The current repair adapter prepares reviewed call-allies files in a separate
inactive fixes mod. Inactive undo checks exact owned files and preserves recovery
copies. Original Workshop/vanilla/mod files remain unchanged. Game testing,
normal-playset activation and live undo remain unavailable and are not registered
as callable tools.

## State migration and qualification

New state lives under OS-local `personal-lab/instances/`. Explicit backed-up
relocation preserves IDs, saved source snapshots, reviews, note bindings and
immutable evidence. It rebases only designated owned operational paths and
invalidates pending authority. `lab restore` creates a new instance identity,
preserves saved discussion, and invalidates old grants/questions/process bindings.
Unresolved external-write journals block restore; `lab recover-restore` handles
interrupted publication without replaying effects. Old roots are retired; do not start old runtimes
against them or share active SQLite/staging state through VM folders.

After a checkout rename, `lab migrate SOURCE DESTINATION --backup BACKUP --previous-source-root /absolute/absent/former/root` can explicitly map saved artifact paths from the former root. The former root must be absent and separate from all current roots. Each mapped relative file must exist in the verified backup. Original databases and immutable evidence retain their historical paths.

Prepared isolation plans contain exact root and process bindings. Relocation
keeps their plan/policy/review bytes intact and makes them historical/ineligible.
A future qualification needs new preparation and explicit approval. This migration
launches no game and establishes no loader, gameplay or Windows conformance.
