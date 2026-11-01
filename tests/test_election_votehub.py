"""Synthetic feed cases test QA boundaries without asserting real poll results."""
from datetime import date
import json

import pytest

from election_model.votehub import (
    CandidateEvidence, ExistingSurvey, PrimaryPollReview, audit_votehub,
)


AS_OF = date(2026, 11, 1)


def source(*changes):
    row = {"id": "synthetic1", "poll_type": "us-senator", "sample_size": 700,
           "population": "lv", "url": "https://example.org/synthetic-poll",
           "created_at": "2026-10-25", "start_date": "2026-10-20", "end_date": "2026-10-22",
           "pollster": "Synthetic Firm", "answers": [{"choice": "Republican Example", "pct": 47.0},
                                                       {"choice": "Democrat Example", "pct": 43.0}],
           "seat_name": None, "sponsors": ["Synthetic publisher"], "internal": False,
           "partisan": None, "subject": "2026 Synthetic"}
    return json.dumps([row | change for change in changes] if changes else [row]).encode()


def evidence():
    return [CandidateEvidence(subject="2026 Synthetic", choice=f"{name} Example", race_id="2026-PA-SEN",
                              chamber="senate", state="PA", party=party, candidate_id=f"synthetic-{party}",
                              source_name=f"EXAMPLE, {name.upper()}", evidence_date=AS_OF,
                              source_url="https://example.org/synthetic-primary-party-evidence")
            for name, party in [("Democrat", "DEM"), ("Republican", "REP")]]


def audit(raw=None, **kwargs):
    return audit_votehub(raw or source(), as_of=AS_OF, candidate_evidence=kwargs.pop("candidate_evidence", evidence()),
                         **kwargs)


def test_party_evidence_not_answer_order_determines_margin():
    result = audit()
    assert not result.reviewed_polls
    assert len(result.candidate_verified_polls) == 1
    poll = result.candidate_verified_polls[0]
    assert poll.dem_pct == 43 and poll.rep_pct == 47
    assert poll.sample_size_verified is False
    assert poll.availability_basis == "provider_created_at_proxy"
    assert poll.source_url == "https://example.org/synthetic-poll"
    assert poll.internal is False and poll.partisan is False
    assert poll.sponsors == ["Synthetic publisher"]
    assert result.rows[0].status == "candidate_verified"
    assert len(result.rows[0].verified_candidate_ids) == 2


def test_missing_or_inexact_candidate_evidence_is_rejected():
    result = audit(candidate_evidence=[])
    assert not result.candidate_verified_polls
    assert "candidate_party_not_verified" in result.rows[0].reasons
    assert result.rows[0].unverified_choices == ["Republican Example", "Democrat Example"]
    result = audit(source({"answers": [{"choice": "Democrat", "pct": 43.0},
                                      {"choice": "Republican Example", "pct": 47.0}]}))
    assert not result.candidate_verified_polls


def test_independent_not_recoded_to_democrat():
    candidates = evidence()
    candidates[0] = candidates[0].model_copy(update={"party": "IND"})
    result = audit(candidate_evidence=candidates)
    assert "requires_one_verified_democrat_and_republican" in result.rows[0].reasons
    assert not result.candidate_verified_polls


def test_cutoff_and_flagged_polls_are_explicit_rejections():
    result = audit(source({"created_at": "2026-11-02"}, {"id": "synthetic2", "internal": True},
                          {"id": "synthetic3", "partisan": "DEM"}))
    assert "not_available_by_cutoff" in result.rows[0].reasons
    assert "internal_poll_excluded" in result.rows[1].reasons
    assert "partisan_poll_excluded" in result.rows[2].reasons
    assert not result.candidate_verified_polls


def test_same_survey_variants_require_unique_primary_selection():
    raw = source({}, {"id": "synthetic2"})
    result = audit(raw)
    assert not result.candidate_verified_polls
    assert result.rejection_counts["ambiguous_same_survey_variant"] == 2
    review = PrimaryPollReview(poll_id="synthetic1", available_date=date(2026, 10, 24), sample_size=600,
                               population="lv", dem_pct=43.0, rep_pct=47.0,
                               source_url="https://example.org/synthetic-primary-poll")
    result = audit(raw, primary_reviews=[review])
    assert len(result.reviewed_polls) == 1
    assert result.reviewed_polls[0].sample_size == 600
    assert result.reviewed_polls[0].sample_size_verified is True
    assert result.reviewed_polls[0].availability_basis == "publisher_release"
    assert result.rows[1].status == "rejected"


def test_existing_primary_survey_suppresses_provider_leaned_duplicate():
    existing = ExistingSurvey(race_id="2026-PA-SEN", pollster="Synthetic Firm Polling",
                              start_date=date(2026, 10, 20), end_date=date(2026, 10, 22), poll_id="primary")
    result = audit(existing_surveys=[existing], pollster_aliases={"Synthetic Firm": "Synthetic Firm Polling"})
    assert not result.candidate_verified_polls
    assert "overlaps_existing_primary_survey" in result.rows[0].reasons


def test_unverified_missing_sample_uses_explicit_placeholder():
    result = audit(source({"sample_size": None}))
    assert result.candidate_verified_polls[0].sample_size == 1000
    assert result.candidate_verified_polls[0].sample_size_verified is False
    assert result.rows[0].sample_size_reported is None
    assert any("placeholder" in warning for warning in result.rows[0].warnings)


def test_invalid_date_row_is_audited_without_suppressing_valid_row():
    result = audit(source({}, {"id": "synthetic2", "created_at": "2026-10-21",
                              "end_date": "2026-10-23"}))
    assert len(result.candidate_verified_polls) == 1
    assert result.rows[1].status == "rejected"
    assert "invalid_field_or_provider_dates" in result.rows[1].reasons


def test_alaska_first_choice_share_not_used_as_final_outcome():
    candidates = [item.model_copy(update={"subject": "2026 Alaska", "race_id": "2026-AK-SEN", "state": "AK"})
                  for item in evidence()]
    result = audit(source({"subject": "2026 Alaska"}), candidate_evidence=candidates)
    assert "ranked_choice_final_outcome_unsupported" in result.rows[0].reasons
    assert not result.candidate_verified_polls


def test_duplicate_identity_and_unbounded_or_nonfinite_source_rejected():
    with pytest.raises(ValueError, match="IDs must be unique"):
        audit(source({}, {}))
    with pytest.raises(ValueError, match="at most 5,000"):
        audit(json.dumps([{}] * 5001).encode())
    with pytest.raises(ValueError):
        audit(source({"answers": [{"choice": "Democrat Example", "pct": float("nan")},
                                   {"choice": "Republican Example", "pct": 47.0}]}))


def test_primary_values_disagreement_is_not_silently_overridden():
    review = PrimaryPollReview(poll_id="synthetic1", available_date=date(2026, 10, 24), sample_size=600,
                               population="lv", dem_pct=44.0, rep_pct=47.0,
                               source_url="https://example.org/synthetic-primary-poll")
    result = audit(primary_reviews=[review])
    assert not result.reviewed_polls
    assert "primary_source_values_disagree" in result.rows[0].reasons
