"""Thin trusted host registration for released, observed US election results.

Operations are read-only observations over published releases in the operator-
configured ``election_data_root``. They accept identifiers and bounded pages, never
paths, URLs or credentials. Acquisition, import and publication stay CLI-only.
"""
from functools import wraps

from toolkit_core.contracts import LabError, OperationSpec


def build_operations(application):
    from election_data.contracts import (ContestsReport, ContestsRequest, IssuesReport, IssuesRequest, MapReport,
                                         MapRequest, ProvenanceReport, ProvenanceRequest, ResultsReport,
                                         ResultsRequest, StatusReport, StatusRequest)
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
             "List contests in a published release with sourced outcomes, electoral votes and coverage cells.",
             ContestsRequest, ContestsReport, reader.contests),
        spec("election_data_results",
             "Page state or county results for one contest with shares, denominators, status and provenance IDs.",
             ResultsRequest, ResultsReport, reader.results),
        spec("election_data_provenance",
             "Trace a published result's derivation to archived source rows, hashes, locators and licenses.",
             ProvenanceRequest, ProvenanceReport, reader.provenance),
        spec("election_data_map",
             "Return a contest's map asset reference and per-feature join status, leader and margin.",
             MapRequest, MapReport, reader.map),
        spec("election_data_issues",
             "Page validation issues and their curation resolutions for a release.",
             IssuesRequest, IssuesReport, reader.issues),
    ]
