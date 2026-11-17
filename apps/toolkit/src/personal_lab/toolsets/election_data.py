"""Thin trusted host registration for released, observed US election results.

Operations are read-only observations over published releases in the operator-
configured ``election_data_root``. They accept identifiers and bounded pages, never
paths, URLs or credentials. Acquisition, import and publication stay CLI-only.
"""
from functools import wraps

from toolkit_core.contracts import LabError, OperationSpec


def build_operations(application):
    from election_data.contracts import (CompareReport, CompareRequest, ContestsReport, ContestsRequest,
                                         CoverageReport, CoverageRequest, IssuesReport, IssuesRequest, MapReport,
                                         MapRequest, PresidentSummaryReport, PresidentSummaryRequest,
                                         ProvenanceReport, ProvenanceRequest, ResultsReport, ResultsRequest,
                                         StatusReport, StatusRequest)
    from election_data.core import ElectionDataError
    from election_data.query import ElectionDataReader, NotFound

    configured = application.configuration.election_data_root
    reader = ElectionDataReader(configured)

    def translated(handler):
        @wraps(handler)
        def call(arguments):
            try:
                return handler(arguments)
            except NotFound as error:
                raise LabError("NOT_FOUND", str(error)) from error
            except ElectionDataError as error:
                code = "CAPABILITY_UNAVAILABLE" if "configured" in str(error) else error.code
                raise LabError(code, str(error)) from error
        return call

    def spec(name, description, request, response, handler):
        return OperationSpec(name, description, request, response, translated(handler), read_only=True,
                             effect="observation")

    return [
        spec("election_data_status",
             "Inspect the local election results archive: active release, sources, licenses and coverage limits.",
             StatusRequest, StatusReport, reader.status),
        spec("election_data_contests",
             "List contests by office, cycle year and state with outcomes, electoral votes and coverage cells.",
             ContestsRequest, ContestsReport, reader.contests),
        spec("election_data_results",
             "Page state, district or county results for one contest with shares, denominators, status and provenance IDs.",
             ResultsRequest, ResultsReport, reader.results),
        spec("election_data_provenance",
             "Trace a published result's derivation to archived source rows, hashes, locators and licenses.",
             ProvenanceRequest, ProvenanceReport, reader.provenance),
        spec("election_data_map",
             "Map asset reference and per-feature status, leader and margin for one contest or a national office-year view.",
             MapRequest, MapReport, reader.map),
        spec("election_data_coverage",
             "Page the coverage inventory by office and cycle: expected contests, what is available, what is missing and why.",
             CoverageRequest, CoverageReport, reader.coverage),
        spec("election_data_president_summary",
             "National popular totals by ticket and actual electoral votes for one presidential election year.",
             PresidentSummaryRequest, PresidentSummaryReport, reader.president_summary),
        spec("election_data_compare",
             "Compare presidential margins between two cycles on identical state or county units; no reallocation.",
             CompareRequest, CompareReport, reader.compare),
        spec("election_data_issues",
             "Page validation issues and their curation resolutions for a release.",
             IssuesRequest, IssuesReport, reader.issues),
    ]
