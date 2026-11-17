-- Schema version 1: logical schema of ELECTION_DATA_TECHNICAL_SPEC section 5.
-- IDs are TEXT, timestamps UTC ISO-8601 TEXT, counts/booleans INTEGER.
-- Source-specific metadata may use validated JSON; join fields never do.

CREATE TABLE schema_migration (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
) STRICT;

-- 5.3 provenance (declared first because reference tables cite artifacts)
CREATE TABLE license_record (
  license_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT,
  text_hash TEXT,
  access_notes TEXT NOT NULL,
  redistribution_status TEXT NOT NULL CHECK (redistribution_status IN
    ('permitted', 'permitted_with_attribution', 'private_use_only', 'unknown')),
  reviewed_at TEXT NOT NULL
) STRICT;

CREATE TABLE source (
  source_id TEXT PRIMARY KEY,
  publisher TEXT NOT NULL,
  landing_url TEXT NOT NULL,
  authority_kind TEXT NOT NULL CHECK (authority_kind IN
    ('official_compilation', 'official_certificate', 'official_canvass', 'academic_secondary',
     'official_reference_geometry')),
  license_id TEXT NOT NULL REFERENCES license_record(license_id),
  contact_or_documentation_url TEXT,
  attribution TEXT NOT NULL,
  access_conditions TEXT NOT NULL
) STRICT;

CREATE TABLE source_artifact (
  artifact_id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
  archive_path TEXT NOT NULL,
  byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
  mime_type TEXT,
  format TEXT NOT NULL,
  original_filename TEXT,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE artifact_retrieval (
  retrieval_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES source(source_id),
  artifact_key TEXT NOT NULL,
  artifact_id TEXT REFERENCES source_artifact(artifact_id),
  requested_url TEXT NOT NULL,
  resolved_url TEXT,
  retrieved_at TEXT NOT NULL,
  published_at TEXT,
  http_status INTEGER,
  etag TEXT,
  last_modified TEXT,
  content_length INTEGER,
  mime_type TEXT,
  status TEXT NOT NULL CHECK (status IN
    ('new_artifact', 'unchanged', 'not_modified', 'manual_import', 'failed')),
  detail TEXT
) STRICT;
CREATE INDEX artifact_retrieval_source ON artifact_retrieval(source_id, artifact_key, retrieved_at);

-- 5.1 reference and geography
CREATE TABLE office (
  office_id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  level TEXT NOT NULL,
  default_selection_method TEXT NOT NULL
) STRICT;

CREATE TABLE geographic_entity (
  geo_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  natural_key TEXT NOT NULL UNIQUE
) STRICT;

CREATE TABLE geographic_version (
  geo_version_id TEXT PRIMARY KEY,
  geo_id TEXT NOT NULL REFERENCES geographic_entity(geo_id),
  name TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  source_artifact_id TEXT REFERENCES source_artifact(artifact_id),
  status TEXT NOT NULL,
  CHECK (valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE geographic_identifier (
  identifier_id TEXT PRIMARY KEY,
  geo_version_id TEXT NOT NULL REFERENCES geographic_version(geo_version_id),
  scheme TEXT NOT NULL,
  value TEXT NOT NULL,
  UNIQUE (scheme, value, geo_version_id)
) STRICT;
CREATE INDEX geographic_identifier_lookup ON geographic_identifier(scheme, value);

CREATE TABLE geographic_relation (
  relation_id TEXT PRIMARY KEY,
  child_version_id TEXT NOT NULL REFERENCES geographic_version(geo_version_id),
  parent_version_id TEXT NOT NULL REFERENCES geographic_version(geo_version_id),
  relation_type TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  source_artifact_id TEXT REFERENCES source_artifact(artifact_id)
) STRICT;

CREATE TABLE boundary_set (
  boundary_set_id TEXT PRIMARY KEY,
  publisher TEXT NOT NULL,
  vintage TEXT NOT NULL,
  layer TEXT NOT NULL,
  crs TEXT NOT NULL,
  source_artifact_id TEXT NOT NULL REFERENCES source_artifact(artifact_id),
  license_id TEXT NOT NULL REFERENCES license_record(license_id),
  quality_status TEXT NOT NULL
) STRICT;

CREATE TABLE boundary_feature (
  feature_id TEXT PRIMARY KEY,
  boundary_set_id TEXT NOT NULL REFERENCES boundary_set(boundary_set_id),
  geo_version_id TEXT NOT NULL REFERENCES geographic_version(geo_version_id),
  source_feature_key TEXT NOT NULL,
  geometry_asset_path TEXT,
  geometry_hash TEXT NOT NULL,
  bbox_json TEXT NOT NULL CHECK (json_valid(bbox_json)),
  geometry_json TEXT NOT NULL CHECK (json_valid(geometry_json)),
  UNIQUE (boundary_set_id, source_feature_key)
) STRICT;

CREATE TABLE reporting_unit (
  unit_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('nation', 'state', 'county', 'county_equivalent', 'state_legislative_district',
     'election_jurisdiction', 'congressional_district', 'precinct', 'nongeographic')),
  geo_version_id TEXT REFERENCES geographic_version(geo_version_id),
  parent_unit_id TEXT REFERENCES reporting_unit(unit_id),
  label TEXT NOT NULL,
  source_identifier TEXT NOT NULL,
  natural_key TEXT NOT NULL UNIQUE,
  notes TEXT
) STRICT;
CREATE INDEX reporting_unit_parent ON reporting_unit(parent_unit_id, kind);

CREATE TABLE reporting_unit_component (
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  geo_version_id TEXT NOT NULL REFERENCES geographic_version(geo_version_id),
  role TEXT NOT NULL,
  PRIMARY KEY (unit_id, geo_version_id, role)
) STRICT;

CREATE TABLE party (
  party_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  natural_key TEXT NOT NULL UNIQUE
) STRICT;

CREATE TABLE party_alias (
  party_id TEXT NOT NULL REFERENCES party(party_id),
  source_id TEXT NOT NULL REFERENCES source(source_id),
  alias TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  PRIMARY KEY (source_id, alias)
) STRICT;

CREATE TABLE person (
  person_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  natural_key TEXT NOT NULL UNIQUE,
  provisional INTEGER NOT NULL CHECK (provisional IN (0, 1))
) STRICT;

CREATE TABLE person_alias (
  person_id TEXT NOT NULL REFERENCES person(person_id),
  source_id TEXT NOT NULL REFERENCES source(source_id),
  alias TEXT NOT NULL,
  PRIMARY KEY (source_id, alias)
) STRICT;

-- 5.2 elections and contests
CREATE TABLE election_event (
  election_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  date_precision TEXT NOT NULL,
  cycle_year INTEGER NOT NULL,
  jurisdiction_geo_id TEXT NOT NULL REFERENCES geographic_entity(geo_id),
  event_type TEXT NOT NULL,
  natural_key TEXT NOT NULL UNIQUE
) STRICT;

CREATE TABLE contest (
  contest_id TEXT PRIMARY KEY,
  election_id TEXT NOT NULL REFERENCES election_event(election_id),
  office_id TEXT NOT NULL REFERENCES office(office_id),
  seat_key TEXT NOT NULL,
  scope_geo_version_id TEXT NOT NULL REFERENCES geographic_version(geo_version_id),
  stage TEXT NOT NULL CHECK (stage IN ('general', 'runoff', 'primary', 'special_general')),
  contest_type TEXT NOT NULL CHECK (contest_type IN ('regular', 'special')),
  selection_method TEXT NOT NULL,
  voting_system TEXT NOT NULL,
  seats_to_fill INTEGER NOT NULL CHECK (seats_to_fill >= 1),
  predecessor_contest_id TEXT REFERENCES contest(contest_id),
  term_start TEXT,
  term_end TEXT,
  label TEXT NOT NULL,
  UNIQUE (election_id, office_id, seat_key, stage, contest_type)
) STRICT;
CREATE INDEX contest_lookup ON contest(office_id, stage, scope_geo_version_id);

CREATE TABLE candidacy (
  candidacy_id TEXT PRIMARY KEY,
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  display_name TEXT NOT NULL,
  candidacy_kind TEXT NOT NULL CHECK (candidacy_kind IN ('ticket', 'candidate', 'aggregate')),
  primary_party_id TEXT REFERENCES party(party_id),
  status TEXT NOT NULL,
  natural_key TEXT NOT NULL,
  UNIQUE (contest_id, natural_key)
) STRICT;

CREATE TABLE candidacy_member (
  candidacy_id TEXT NOT NULL REFERENCES candidacy(candidacy_id),
  person_id TEXT NOT NULL REFERENCES person(person_id),
  role TEXT NOT NULL CHECK (role IN ('president', 'vice_president', 'candidate')),
  PRIMARY KEY (candidacy_id, person_id, role)
) STRICT;

CREATE TABLE ballot_option (
  option_id TEXT PRIMARY KEY,
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  candidacy_id TEXT REFERENCES candidacy(candidacy_id),
  party_id TEXT REFERENCES party(party_id),
  option_kind TEXT NOT NULL CHECK (option_kind IN
    ('candidacy_total', 'party_line', 'aggregate_other', 'aggregate_write_in', 'none_of_these')),
  label TEXT NOT NULL,
  natural_key TEXT NOT NULL,
  UNIQUE (contest_id, natural_key)
) STRICT;

CREATE TABLE electoral_allocation (
  allocation_id TEXT PRIMARY KEY,
  election_id TEXT NOT NULL REFERENCES election_event(election_id),
  geo_version_id TEXT NOT NULL REFERENCES geographic_version(geo_version_id),
  allocation_kind TEXT NOT NULL CHECK (allocation_kind IN ('state_total', 'statewide_at_large', 'congressional_district')),
  component_key TEXT NOT NULL,
  elector_count INTEGER NOT NULL CHECK (elector_count >= 0),
  source_artifact_id TEXT NOT NULL REFERENCES source_artifact(artifact_id),
  UNIQUE (election_id, geo_version_id, allocation_kind, component_key)
) STRICT;

-- 5.3 continued: ingestion and observed facts
CREATE TABLE import_run (
  run_id TEXT PRIMARY KEY,
  import_identity TEXT NOT NULL,
  adapter_id TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  code_revision TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  mapping_version TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'superseded')),
  output_hash TEXT,
  row_accounting_json TEXT CHECK (row_accounting_json IS NULL OR json_valid(row_accounting_json)),
  environment_manifest_json TEXT NOT NULL CHECK (json_valid(environment_manifest_json)),
  diagnostics_json TEXT CHECK (diagnostics_json IS NULL OR json_valid(diagnostics_json))
) STRICT;
CREATE UNIQUE INDEX import_run_identity_completed ON import_run(import_identity) WHERE status = 'completed';

CREATE TABLE run_artifact (
  run_id TEXT NOT NULL REFERENCES import_run(run_id),
  artifact_id TEXT NOT NULL REFERENCES source_artifact(artifact_id),
  role TEXT NOT NULL,
  PRIMARY KEY (run_id, artifact_id, role)
) STRICT;

CREATE TABLE source_result_set (
  set_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES import_run(run_id),
  source_id TEXT NOT NULL REFERENCES source(source_id),
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  revision_label TEXT NOT NULL,
  source_status TEXT NOT NULL CHECK (source_status IN
    ('certified', 'official_compilation_of_certified', 'secondary_compilation', 'unofficial', 'unknown')),
  as_of_at TEXT,
  certified_at TEXT,
  tally_basis TEXT NOT NULL,
  geography_partition_key TEXT NOT NULL,
  notes TEXT,
  UNIQUE (run_id, contest_id, geography_partition_key)
) STRICT;

-- batch_key separates source rows repeated at otherwise identical grain (file order:
-- b1, b2, ...). Batches are never summed except under an explicit curation decision.
CREATE TABLE vote_observation (
  observation_id TEXT PRIMARY KEY,
  set_id TEXT NOT NULL REFERENCES source_result_set(set_id),
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  option_id TEXT NOT NULL REFERENCES ballot_option(option_id),
  round_key TEXT NOT NULL,
  voting_mode TEXT NOT NULL,
  batch_key TEXT NOT NULL,
  votes INTEGER CHECK (votes IS NULL OR votes >= 0),
  value_status TEXT NOT NULL CHECK
    (value_status IN ('reported', 'missing', 'suppressed', 'not_applicable')),
  source_row_key TEXT NOT NULL,
  raw_candidate_label TEXT,
  raw_party_label TEXT,
  CHECK ((value_status = 'reported' AND votes IS NOT NULL)
      OR (value_status <> 'reported' AND votes IS NULL)),
  UNIQUE (set_id, unit_id, option_id, round_key, voting_mode, batch_key)
) STRICT;
CREATE INDEX vote_observation_row ON vote_observation(set_id, source_row_key);

CREATE TABLE metric_observation (
  metric_id TEXT PRIMARY KEY,
  set_id TEXT NOT NULL REFERENCES source_result_set(set_id),
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  round_key TEXT NOT NULL,
  voting_mode TEXT NOT NULL,
  batch_key TEXT NOT NULL,
  metric_code TEXT NOT NULL,
  integer_value INTEGER CHECK (integer_value IS NULL OR integer_value >= 0),
  value_status TEXT NOT NULL CHECK
    (value_status IN ('reported', 'missing', 'suppressed', 'not_applicable')),
  source_row_key TEXT NOT NULL,
  CHECK ((value_status = 'reported' AND integer_value IS NOT NULL)
      OR (value_status <> 'reported' AND integer_value IS NULL)),
  UNIQUE (set_id, unit_id, round_key, voting_mode, metric_code, batch_key)
) STRICT;

CREATE TABLE observation_evidence (
  observation_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES source_artifact(artifact_id),
  locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
  PRIMARY KEY (observation_id, artifact_id)
) STRICT;

CREATE TABLE electoral_vote (
  electoral_vote_id TEXT PRIMARY KEY,
  allocation_id TEXT NOT NULL REFERENCES electoral_allocation(allocation_id),
  recipient_person_id TEXT REFERENCES person(person_id),
  vote_role TEXT NOT NULL CHECK (vote_role IN ('president', 'vice_president')),
  votes INTEGER NOT NULL CHECK (votes >= 0),
  source_artifact_id TEXT NOT NULL REFERENCES source_artifact(artifact_id),
  run_id TEXT NOT NULL REFERENCES import_run(run_id),
  locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
  UNIQUE (run_id, allocation_id, recipient_person_id, vote_role)
) STRICT;

CREATE TABLE validation_issue (
  issue_id TEXT PRIMARY KEY,
  run_id TEXT REFERENCES import_run(run_id),
  release_id TEXT,
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  expected_json TEXT CHECK (expected_json IS NULL OR json_valid(expected_json)),
  actual_json TEXT CHECK (actual_json IS NULL OR json_valid(actual_json)),
  message TEXT NOT NULL,
  resolution_status TEXT NOT NULL CHECK (resolution_status IN ('open', 'accepted', 'blocked')),
  resolution_note TEXT,
  decision_id TEXT
) STRICT;
CREATE INDEX validation_issue_scope ON validation_issue(run_id, release_id, severity);

CREATE TABLE curation_decision (
  decision_id TEXT PRIMARY KEY,
  decision_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  rationale TEXT NOT NULL,
  evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
  author TEXT NOT NULL,
  created_at TEXT NOT NULL,
  supersedes_id TEXT REFERENCES curation_decision(decision_id),
  content_hash TEXT NOT NULL
) STRICT;

-- 5.4 releases and serving
CREATE TABLE data_release (
  release_id TEXT PRIMARY KEY,
  parent_release_id TEXT REFERENCES data_release(release_id),
  created_at TEXT NOT NULL,
  published_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('building', 'staged', 'published', 'failed')),
  schema_version INTEGER NOT NULL,
  manifest_hash TEXT,
  scope_key TEXT NOT NULL,
  notes TEXT
) STRICT;

CREATE TABLE release_selection (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  series_key TEXT NOT NULL,
  set_id TEXT NOT NULL REFERENCES source_result_set(set_id),
  decision_id TEXT NOT NULL REFERENCES curation_decision(decision_id),
  PRIMARY KEY (release_id, contest_id, unit_id, series_key)
) STRICT;

CREATE TABLE derivation (
  derivation_id TEXT PRIMARY KEY,
  rule_version TEXT NOT NULL,
  input_ids_json TEXT CHECK (input_ids_json IS NULL OR json_valid(input_ids_json)),
  parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
  output_hash TEXT NOT NULL
) STRICT;

CREATE TABLE derivation_input (
  derivation_id TEXT NOT NULL REFERENCES derivation(derivation_id),
  input_kind TEXT NOT NULL CHECK (input_kind IN ('vote_observation', 'metric_observation')),
  input_id TEXT NOT NULL,
  PRIMARY KEY (derivation_id, input_kind, input_id)
) STRICT;

CREATE TABLE published_result (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  candidacy_id TEXT NOT NULL REFERENCES candidacy(candidacy_id),
  round_key TEXT NOT NULL,
  voting_mode TEXT NOT NULL,
  votes INTEGER CHECK (votes IS NULL OR votes >= 0),
  denominator INTEGER CHECK (denominator IS NULL OR denominator > 0),
  denominator_kind TEXT,
  result_status TEXT NOT NULL CHECK (result_status IN ('reported', 'missing', 'incomplete_options')),
  derivation_id TEXT NOT NULL REFERENCES derivation(derivation_id),
  PRIMARY KEY (release_id, contest_id, unit_id, candidacy_id, round_key, voting_mode)
) STRICT;
CREATE INDEX published_result_unit ON published_result(release_id, unit_id, contest_id);

CREATE TABLE published_unit_status (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  geography_level TEXT NOT NULL,
  result_status TEXT NOT NULL CHECK (result_status IN ('complete', 'partial', 'unavailable')),
  boundary_feature_id TEXT REFERENCES boundary_feature(feature_id),
  boundary_status TEXT NOT NULL CHECK (boundary_status IN ('matched', 'boundary_unavailable', 'not_applicable')),
  source_status TEXT NOT NULL,
  notes_json TEXT NOT NULL CHECK (json_valid(notes_json)),
  PRIMARY KEY (release_id, contest_id, unit_id)
) STRICT;

CREATE TABLE published_outcome (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  candidacy_id TEXT REFERENCES candidacy(candidacy_id),
  outcome_type TEXT NOT NULL CHECK (outcome_type IN ('won_electors', 'plurality_leader', 'unresolved')),
  basis TEXT NOT NULL,
  source_artifact_id TEXT REFERENCES source_artifact(artifact_id),
  notes TEXT,
  PRIMARY KEY (release_id, contest_id, outcome_type)
) STRICT;

CREATE TABLE published_electoral_vote (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  electoral_vote_id TEXT NOT NULL REFERENCES electoral_vote(electoral_vote_id),
  PRIMARY KEY (release_id, electoral_vote_id)
) STRICT;

CREATE TABLE coverage_cell (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  geography_level TEXT NOT NULL,
  coverage_status TEXT NOT NULL CHECK (coverage_status IN
    ('complete', 'partial', 'unavailable', 'not_researched', 'not_held', 'not_applicable')),
  expected_units INTEGER,
  available_units INTEGER NOT NULL,
  missing_units_json TEXT NOT NULL CHECK (json_valid(missing_units_json)),
  boundary_status TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY (release_id, contest_id, geography_level)
) STRICT;

CREATE TABLE release_asset (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  asset_id TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  byte_length INTEGER NOT NULL,
  kind TEXT NOT NULL,
  license_id TEXT NOT NULL REFERENCES license_record(license_id),
  PRIMARY KEY (release_id, asset_id)
) STRICT;

CREATE TABLE release_decision (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  decision_id TEXT NOT NULL REFERENCES curation_decision(decision_id),
  PRIMARY KEY (release_id, decision_id)
) STRICT;

CREATE TABLE active_release (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  activated_at TEXT NOT NULL
) STRICT;

-- Cross-table invariants the spec asks for "where practical" in triggers.
CREATE TRIGGER vote_observation_same_contest BEFORE INSERT ON vote_observation
WHEN (SELECT contest_id FROM ballot_option WHERE option_id = NEW.option_id)
  IS NOT (SELECT contest_id FROM source_result_set WHERE set_id = NEW.set_id)
BEGIN SELECT RAISE(ABORT, 'ballot option and result set belong to different contests'); END;

CREATE TRIGGER published_result_requires_release BEFORE INSERT ON published_result
WHEN (SELECT status FROM data_release WHERE release_id = NEW.release_id) NOT IN ('building', 'staged', 'published')
BEGIN SELECT RAISE(ABORT, 'published data must reference a staged or published release'); END;

CREATE TRIGGER active_release_requires_published BEFORE INSERT ON active_release
WHEN (SELECT status FROM data_release WHERE release_id = NEW.release_id) IS NOT 'published'
BEGIN SELECT RAISE(ABORT, 'only a published release can be active'); END;

CREATE TRIGGER active_release_update_requires_published BEFORE UPDATE ON active_release
WHEN (SELECT status FROM data_release WHERE release_id = NEW.release_id) IS NOT 'published'
BEGIN SELECT RAISE(ABORT, 'only a published release can be active'); END;

CREATE TRIGGER source_artifact_immutable BEFORE UPDATE ON source_artifact
BEGIN SELECT RAISE(ABORT, 'archived artifacts are immutable'); END;

CREATE TRIGGER curation_decision_append_only BEFORE UPDATE ON curation_decision
BEGIN SELECT RAISE(ABORT, 'curation decisions are append-only'); END;
