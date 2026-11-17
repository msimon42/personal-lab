"""Acquisition and untrusted-format reader contracts, fully offline."""
from __future__ import annotations

import email.message
import io
import json
import urllib.error
import zipfile
from contextlib import closing

import pytest

from election_data.acquire import Fetcher, _RedirectGuard, fetch_artifact, load_manifest, register_sources
from election_data.core import AcquisitionFailure, DataRoot, InvalidInput, connect, migrate
from election_data.readers import open_bounded_zip, read_html_tables, read_shapefile_zip, read_xlsx
from election_data_support import _square, shapefile_zip, xlsx


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, url: str, status: int = 200, headers: dict | None = None):
        super().__init__(body)
        self.status, self._url = status, url
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self):
        return self._url


class FakeOpener:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def open(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, dict(request.header_items())))
        handler = self.routes[(request.get_method(), request.full_url.split("?")[0])]
        outcome = handler(request) if callable(handler) else handler
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http_error(url, code, headers=None):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "error", message, io.BytesIO(b"detail"))


@pytest.fixture
def root(tmp_path):
    data_root = DataRoot.open(tmp_path / "root", create=True)
    with closing(connect(data_root.database)) as connection:
        migrate(connection)
        register_sources(connection)
    return data_root


def test_manifest_hosts_and_licenses_are_reviewed():
    manifest = load_manifest()
    licenses = {entry["license_id"] for entry in manifest["licenses"]}
    for source in manifest["sources"]:
        assert source["license_id"] in licenses and source["attribution"] and source["access_conditions"]
        for artifact in source["artifacts"]:
            host = artifact["access"].get("host") or artifact["access"]["url"].split("/")[2]
            assert host in manifest["allowed_hosts"] and artifact["max_bytes"] > 0


def test_fetcher_refuses_unlisted_hosts_and_redirects():
    fetcher = Fetcher({"www.fec.gov"}, sleep=lambda seconds: None, opener=FakeOpener({}))
    with pytest.raises(AcquisitionFailure, match="not an allowed"):
        fetcher.request("https://evil.example/x", destination=None, max_bytes=10)
    with pytest.raises(AcquisitionFailure, match="not an allowed"):
        fetcher.request("http://www.fec.gov/x", destination=None, max_bytes=10)
    guard = _RedirectGuard({"www.fec.gov"})
    with pytest.raises(AcquisitionFailure, match="redirect"):
        guard.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/file")


def test_size_limits_retries_and_conditional_requests(root, tmp_path):
    url = "https://www.fec.gov/documents/5645/2024presgeresults.xlsx"
    sleeps = []
    attempts = iter([_http_error(url, 503, {"Retry-After": "2"}),
                     FakeResponse(b"workbook-bytes", url, headers={"ETag": '"v1"', "Last-Modified": "Mon"})])
    opener = FakeOpener({("GET", url): lambda request: next(attempts)})
    fetcher = Fetcher({"www.fec.gov"}, sleep=sleeps.append, opener=opener)
    with closing(connect(root.database)) as connection:
        first = fetch_artifact(root, connection, "fec-federal-elections-2024-president", "2024presgeresults.xlsx",
                               fetcher=fetcher)
        assert first["status"] == "new_artifact" and 2.0 in sleeps
        opener.routes[("GET", url)] = _http_error(url, 304)
        second = fetch_artifact(root, connection, "fec-federal-elections-2024-president", "2024presgeresults.xlsx",
                                fetcher=fetcher)
        assert second == {"artifact_id": first["artifact_id"], "status": "not_modified"}
        assert opener.calls[-1][2].get("If-none-match") == '"v1"'
        opener.routes[("GET", url)] = FakeResponse(b"x" * 6_000_000, url)
        with pytest.raises(AcquisitionFailure, match="limit"):
            fetch_artifact(root, connection, "fec-federal-elections-2024-president", "2024presgeresults.xlsx",
                           fetcher=fetcher)
        statuses = [row[0] for row in connection.execute("SELECT status FROM artifact_retrieval ORDER BY rowid")]
        artifacts = connection.execute("SELECT count(*) FROM source_artifact").fetchone()[0]
    assert statuses == ["new_artifact", "not_modified", "failed"] and artifacts == 1
    assert not list((root.path / "staging").iterdir())


def test_dataverse_guestbook_identity_is_local_and_unchanged_files_are_not_refetched(root):
    host = "https://dataverse.harvard.edu"
    metadata = json.dumps({"data": {"version": 3, "dataFile": {"checksum": {"type": "MD5", "value": "abc"},
                                                               "filesize": 5, "publicationDate": "2026-02-25"}}})
    posted = []

    def guestbook(request):
        posted.append(json.loads(request.data))
        return FakeResponse(json.dumps({"data": {"signedUrl": f"{host}/api/v1/access/datafile/13573089?token=SECRET"}}
                                       ).encode(), request.full_url)

    opener = FakeOpener({
        ("GET", f"{host}/api/files/13573089"): lambda request: FakeResponse(metadata.encode(), request.full_url),
        ("POST", f"{host}/api/access/datafile/13573089"): guestbook,
        ("GET", f"{host}/api/v1/access/datafile/13573089"): lambda request: FakeResponse(b"a\tb\n", request.full_url),
    })
    fetcher = Fetcher({"dataverse.harvard.edu"}, sleep=lambda seconds: None, opener=opener)
    with closing(connect(root.database)) as connection:
        with pytest.raises(AcquisitionFailure, match="guestbook"):
            fetch_artifact(root, connection, "mit-county-president", "countypres_2000-2024.tab", fetcher=fetcher)
        (root.path / "config" / "local.toml").write_text(
            '[dataverse_guestbook]\nname = "Test Person"\nemail = "person@example.invalid"\n'
            'institution = "Independent"\nposition = "Testing"\n', encoding="utf-8")
        first = fetch_artifact(root, connection, "mit-county-president", "countypres_2000-2024.tab", fetcher=fetcher)
        second = fetch_artifact(root, connection, "mit-county-president", "countypres_2000-2024.tab", fetcher=fetcher)
        recorded = [row[0] for row in connection.execute("SELECT coalesce(resolved_url, '') || requested_url "
                                                         "FROM artifact_retrieval")]
    assert first["status"] == "new_artifact" and second["status"] == "not_modified"
    assert len(posted) == 1 and posted[0]["guestbookResponse"]["institution"] == "Independent"
    assert not any("SECRET" in value or "token=" in value for value in recorded)


def test_bounded_zip_rejects_traversal_and_bombs(tmp_path):
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.shp", b"x")
    with pytest.raises(InvalidInput, match="unsafe"):
        open_bounded_zip(traversal)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.dbf", b"\0" * 5_000_000)
    with pytest.raises(InvalidInput, match="compression ratio"):
        open_bounded_zip(bomb)


def test_shapefile_rings_follow_rfc7946_and_holes_attach_to_exteriors(tmp_path):
    hole = list(reversed(_square(0.25, 0.25, 0.5, 0.5)))
    records = [({"GEOID": "01", "NAME": "With hole"}, [_square(0, 0, 1, 1), hole]),
               ({"GEOID": "02", "NAME": "Two parts"}, [_square(2, 0, 1, 1), _square(4, 0, 1, 1)])]
    path = tmp_path / "layer.zip"
    path.write_bytes(shapefile_zip("layer", records))
    features = read_shapefile_zip(path, "layer")
    first, second = features[0]["geometry"], features[1]["geometry"]
    assert first["type"] == "Polygon" and len(first["coordinates"]) == 2
    exterior = first["coordinates"][0]
    signed = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(exterior, exterior[1:])) / 2
    assert signed > 0  # counter-clockwise exterior
    assert second["type"] == "MultiPolygon" and len(second["coordinates"]) == 2
    assert features[0]["properties"] == {"GEOID": "01", "NAME": "With hole"}


def test_xlsx_and_html_readers_return_raw_text(tmp_path):
    workbook = tmp_path / "book.xlsx"
    workbook.write_bytes(xlsx("Sheet", [["STATE", "VOTES"], ["AL", 9], [None, "note"]]))
    rows = read_xlsx(workbook)
    assert rows[1] == {"sheet": "Sheet", "row": 2, "cells": {"A": "AL", "B": "9"}}
    assert rows[2]["cells"] == {"B": "note"}
    page = tmp_path / "page.html"
    page.write_text("<table><tr><th>A&nbsp;B</th><td>1<br>2</td></tr></table><script>alert(1)</script>",
                    encoding="utf-8")
    assert read_html_tables(page) == [[["A B", "1 2"]]]  # whitespace, including NBSP, is collapsed
