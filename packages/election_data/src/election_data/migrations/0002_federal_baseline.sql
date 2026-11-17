-- Schema version 2: multi-office federal baseline.
-- Adds the office-by-cycle coverage inventory, a "reference_vintage" boundary status for
-- results joined to a later boundary vintage, and lookup indexes for many contests.

CREATE TABLE coverage_inventory (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  office_code TEXT NOT NULL,
  cycle_year INTEGER NOT NULL,
  series TEXT NOT NULL CHECK (series IN ('contest_totals', 'county_detail', 'electoral_votes', 'boundaries')),
  coverage_status TEXT NOT NULL CHECK (coverage_status IN
    ('complete', 'partial', 'unavailable', 'not_researched', 'not_held', 'not_applicable')),
  expected INTEGER,
  available INTEGER NOT NULL,
  missing_json TEXT NOT NULL CHECK (json_valid(missing_json)),
  extra_json TEXT NOT NULL CHECK (json_valid(extra_json)),
  notes TEXT,
  PRIMARY KEY (release_id, office_code, cycle_year, series)
) STRICT;

-- published_unit_status is a rebuildable serving table; widen its boundary_status domain.
CREATE TABLE published_unit_status_v2 (
  release_id TEXT NOT NULL REFERENCES data_release(release_id),
  contest_id TEXT NOT NULL REFERENCES contest(contest_id),
  unit_id TEXT NOT NULL REFERENCES reporting_unit(unit_id),
  geography_level TEXT NOT NULL,
  result_status TEXT NOT NULL CHECK (result_status IN ('complete', 'partial', 'unavailable')),
  boundary_feature_id TEXT REFERENCES boundary_feature(feature_id),
  boundary_status TEXT NOT NULL CHECK (boundary_status IN
    ('matched', 'reference_vintage', 'boundary_unavailable', 'not_applicable')),
  source_status TEXT NOT NULL,
  notes_json TEXT NOT NULL CHECK (json_valid(notes_json)),
  PRIMARY KEY (release_id, contest_id, unit_id)
) STRICT;
INSERT INTO published_unit_status_v2 SELECT * FROM published_unit_status;
DROP TABLE published_unit_status;
ALTER TABLE published_unit_status_v2 RENAME TO published_unit_status;

CREATE INDEX published_unit_status_level ON published_unit_status(release_id, geography_level, contest_id);
CREATE INDEX election_event_cycle ON election_event(cycle_year);
CREATE INDEX contest_election ON contest(election_id, office_id);
CREATE INDEX source_result_set_contest ON source_result_set(contest_id, source_id);
CREATE INDEX vote_observation_set_unit ON vote_observation(set_id, unit_id);
CREATE INDEX metric_observation_set_unit ON metric_observation(set_id, unit_id);
CREATE INDEX published_outcome_contest ON published_outcome(release_id, contest_id);
CREATE INDEX coverage_cell_level ON coverage_cell(release_id, geography_level, coverage_status);
