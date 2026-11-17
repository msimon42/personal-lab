"""Source registry and polite, bounded acquisition into the immutable archive.

Network access happens only here and only to hosts the committed manifest allows.
Downloads stream to a staging file, are size-limited and hashed, then move
atomically into the content-addressed archive. A failed download never touches
published releases.
"""
from __future__ import annotations

import json
import random
import shutil
import sqlite3
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .core import (AcquisitionFailure, DataRoot, InvalidInput, archive_bytes, canonical_json, sha256_text, stable_id,
                   staging_file, transaction, utc_now)

USER_AGENT = "personal-lab-election-data/0.1 (local research archive)"
MAX_ATTEMPTS = 4
MIN_HOST_INTERVAL_SECONDS = 1.0


def load_manifest() -> dict[str, Any]:
    text = resources.files("election_data.manifests").joinpath("sources.json").read_text(encoding="utf-8")
    return json.loads(text)


def register_sources(connection: sqlite3.Connection, manifest: dict[str, Any] | None = None) -> None:
    """Upsert committed source/license records; reviewed manifests are reference data."""
    manifest = manifest or load_manifest()
    reviewed = manifest["reviewed_at"]
    with transaction(connection):
        for license_record in manifest["licenses"]:
            connection.execute(
                "INSERT INTO license_record VALUES (?,?,?,?,?,?,?) ON CONFLICT(license_id) DO UPDATE SET "
                "name=excluded.name, url=excluded.url, access_notes=excluded.access_notes, "
                "redistribution_status=excluded.redistribution_status, reviewed_at=excluded.reviewed_at",
                (license_record["license_id"], license_record["name"], license_record["url"],
                 sha256_text(json.dumps(license_record, sort_keys=True)), license_record["access_notes"],
                 license_record["redistribution_status"], reviewed))
        for source in manifest["sources"]:
            connection.execute(
                "INSERT INTO source VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                "publisher=excluded.publisher, landing_url=excluded.landing_url, "
                "authority_kind=excluded.authority_kind, license_id=excluded.license_id, "
                "contact_or_documentation_url=excluded.contact_or_documentation_url, "
                "attribution=excluded.attribution, access_conditions=excluded.access_conditions",
                (source["source_id"], source["publisher"], source["landing_url"], source["authority_kind"],
                 source["license_id"], source.get("documentation_url"), source["attribution"],
                 source["access_conditions"]))


def find_artifact_spec(source_id: str, artifact_key: str, manifest: dict[str, Any] | None = None) -> dict:
    manifest = manifest or load_manifest()
    for source in manifest["sources"]:
        if source["source_id"] == source_id:
            for artifact in source["artifacts"]:
                if artifact["artifact_key"] == artifact_key:
                    return artifact
            raise InvalidInput(f"Source {source_id} has no artifact {artifact_key}.")
    raise InvalidInput(f"Unknown source {source_id}.")


def load_local_config(root: DataRoot) -> dict[str, Any]:
    """Machine-local settings (for example a guestbook identity). Never committed."""
    path = root.path / "config" / "local.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str]


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname
        if urllib.parse.urlsplit(newurl).scheme != "https" or host not in self.allowed_hosts:
            raise AcquisitionFailure(f"Refusing redirect to non-allowed destination {host}.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """HTTPS client with an allowlist, per-host spacing, bounded retries and size limits."""

    def __init__(self, allowed_hosts: set[str], *, sleep=time.sleep, opener=None):
        self.allowed_hosts = allowed_hosts
        self.sleep = sleep
        self.last_request: dict[str, float] = {}
        self.opener = opener

    def _check(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.allowed_hosts:
            raise AcquisitionFailure(f"Destination {parts.hostname} is not an allowed source host.")
        return parts.hostname

    def _space(self, host: str) -> None:
        elapsed = time.monotonic() - self.last_request.get(host, -1e9)
        if elapsed < MIN_HOST_INTERVAL_SECONDS:
            self.sleep(MIN_HOST_INTERVAL_SECONDS - elapsed)
        self.last_request[host] = time.monotonic()

    def request(self, url: str, *, destination: Path | None, max_bytes: int, method: str = "GET",
                body: bytes | None = None, headers: dict[str, str] | None = None,
                redirect_hosts: set[str] | None = None) -> Response:
        host = self._check(url)
        opener = self.opener or urllib.request.build_opener(_RedirectGuard({host, *(redirect_hosts or set())}))
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._space(host)
            request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
            try:
                with opener.open(request, timeout=60) as response:
                    result = Response(response.status, response.geturl(), {k.lower(): v for k, v in response.headers.items()})
                    declared = result.headers.get("content-length")
                    if declared and int(declared) > max_bytes:
                        raise AcquisitionFailure(f"Declared size {declared} exceeds limit {max_bytes}.")
                    if destination is not None:
                        _stream(response, destination, max_bytes)
                    return result
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    return Response(304, url, {k.lower(): v for k, v in error.headers.items()})
                retryable = error.code == 429 or error.code >= 500
                if not retryable or attempt == MAX_ATTEMPTS:
                    detail = error.read(2000).decode("utf-8", "replace")
                    raise AcquisitionFailure(f"HTTP {error.code} from {host}: {detail[:300]}") from error
                self.sleep(_retry_after(error.headers.get("Retry-After"), attempt))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt == MAX_ATTEMPTS:
                    raise AcquisitionFailure(f"Network failure contacting {host}: {error}") from error
                self.sleep(_retry_after(None, attempt))
        raise AcquisitionFailure("unreachable")


def _retry_after(header: str | None, attempt: int) -> float:
    if header and header.isdigit():
        return min(float(header), 300.0)
    return min(2 ** attempt, 60) + random.uniform(0, 1)


def _stream(response, destination: Path, max_bytes: int) -> None:
    written = 0
    with destination.open("wb") as handle:
        for block in iter(lambda: response.read(1024 * 256), b""):
            written += len(block)
            if written > max_bytes:
                raise AcquisitionFailure(f"Download exceeded the {max_bytes}-byte limit.")
            handle.write(block)


def _latest_retrieval(connection: sqlite3.Connection, source_id: str, artifact_key: str):
    return connection.execute(
        "SELECT * FROM artifact_retrieval WHERE source_id=? AND artifact_key=? AND artifact_id IS NOT NULL "
        "ORDER BY retrieved_at DESC, rowid DESC LIMIT 1", (source_id, artifact_key)).fetchone()


def _record_retrieval(connection, *, source_id, artifact_key, artifact_id, requested_url, response, status,
                      detail=None):
    retrieved_at = utc_now()
    retrieval_id = stable_id("retrieval", source_id, artifact_key, retrieved_at, random.random())
    headers = response.headers if response else {}
    with transaction(connection):
        connection.execute(
            "INSERT INTO artifact_retrieval VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (retrieval_id, source_id, artifact_key, artifact_id, _redact(requested_url),
             _redact(response.url) if response else None, retrieved_at, None,
             response.status if response else None, headers.get("etag"), headers.get("last-modified"),
             int(headers["content-length"]) if headers.get("content-length", "").isdigit() else None,
             headers.get("content-type"), status, detail))
    return retrieval_id


def _redact(url: str) -> str:
    """Signed download URLs carry bearer tokens; keep only scheme, host and path."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def fetch_artifact(root: DataRoot, connection: sqlite3.Connection, source_id: str, artifact_key: str, *,
                   fetcher: Fetcher | None = None, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest()
    spec = find_artifact_spec(source_id, artifact_key, manifest)
    fetcher = fetcher or Fetcher(set(manifest["allowed_hosts"]))
    access = spec["access"]
    previous = _latest_retrieval(connection, source_id, artifact_key)
    temporary = staging_file(root)
    try:
        if access["kind"] == "direct":
            url = access["url"]
            conditional = {}
            if previous is not None and previous["etag"]:
                conditional["If-None-Match"] = previous["etag"]
            if previous is not None and previous["last_modified"]:
                conditional["If-Modified-Since"] = previous["last_modified"]
            response = fetcher.request(url, destination=temporary, max_bytes=spec["max_bytes"],
                                       headers=conditional, redirect_hosts=set(access.get("redirect_hosts", [])))
            if response.status == 304:
                _record_retrieval(connection, source_id=source_id, artifact_key=artifact_key,
                                  artifact_id=previous["artifact_id"], requested_url=url, response=response,
                                  status="not_modified")
                return {"artifact_id": previous["artifact_id"], "status": "not_modified"}
        elif access["kind"] == "dataverse_guestbook":
            # Dataverse offers no conditional GET. Its public file metadata (no guestbook) gives a
            # change token; when it is unchanged we record an observation instead of re-downloading,
            # which would also file another guestbook response.
            token = _dataverse_token(fetcher, access, staging_file(root))
            if previous is not None and previous["detail"] and json.loads(previous["detail"]).get(
                    "dataverse_metadata_token") == token:
                _record_retrieval(connection, source_id=source_id, artifact_key=artifact_key,
                                  artifact_id=previous["artifact_id"],
                                  requested_url=f"https://{access['host']}/api/files/{access['datafile_id']}",
                                  response=None, status="not_modified",
                                  detail=json.dumps({"dataverse_metadata_token": token}))
                return {"artifact_id": previous["artifact_id"], "status": "not_modified"}
            url, response = _dataverse_download(root, fetcher, access, temporary, spec["max_bytes"])
            artifact_id, created = archive_bytes(root, connection, temporary, format=spec["format"],
                                                 mime_type=spec.get("mime_type"), original_filename=artifact_key)
            status = "new_artifact" if created else "unchanged"
            _record_retrieval(connection, source_id=source_id, artifact_key=artifact_key, artifact_id=artifact_id,
                              requested_url=url, response=response, status=status,
                              detail=json.dumps({"dataverse_metadata_token": token}))
            return {"artifact_id": artifact_id, "status": status}
        else:
            raise InvalidInput(f"Unsupported access kind {access['kind']}.")
        artifact_id, created = archive_bytes(root, connection, temporary, format=spec["format"],
                                             mime_type=spec.get("mime_type"), original_filename=artifact_key)
        status = "new_artifact" if created else "unchanged"
        _record_retrieval(connection, source_id=source_id, artifact_key=artifact_key, artifact_id=artifact_id,
                          requested_url=url, response=response, status=status)
        return {"artifact_id": artifact_id, "status": status}
    except AcquisitionFailure as error:
        _record_retrieval(connection, source_id=source_id, artifact_key=artifact_key, artifact_id=None,
                          requested_url=access.get("url", f"https://{access.get('host')}/"), response=None,
                          status="failed", detail=str(error)[:500])
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _dataverse_token(fetcher: Fetcher, access: dict, scratch: Path) -> str:
    """Change token from public file metadata: original-file checksum, size, version and date.

    The checksum describes the uploaded original, not the served bytes, so it is a
    change signal only; archived content identity is always our own SHA-256.
    """
    try:
        fetcher.request(f"https://{access['host']}/api/files/{access['datafile_id']}", destination=scratch,
                        max_bytes=1_000_000)
        data = json.loads(scratch.read_text(encoding="utf-8")).get("data", {})
    finally:
        scratch.unlink(missing_ok=True)
    file = data.get("dataFile", {})
    return canonical_json({"checksum": file.get("checksum"), "filesize": file.get("filesize"),
                           "version": data.get("version"), "published": file.get("publicationDate")})


def _dataverse_download(root: DataRoot, fetcher: Fetcher, access: dict, destination: Path, max_bytes: int):
    identity = load_local_config(root).get("dataverse_guestbook")
    required = ("name", "email", "institution", "position")
    if not identity or any(not identity.get(field) for field in required):
        raise AcquisitionFailure(
            "Dataverse requires a guestbook response. Add [dataverse_guestbook] name, email, institution and "
            f"position to {root.path / 'config' / 'local.toml'}, or import a browser download with "
            "'election-data archive add'.")
    url = f"https://{access['host']}/api/access/datafile/{access['datafile_id']}"
    body = json.dumps({"guestbookResponse": {field: identity[field] for field in required}}).encode()
    signed_destination = destination.with_suffix(".signed.json")
    try:
        fetcher.request(url, destination=signed_destination, max_bytes=100_000, method="POST", body=body,
                        headers={"Content-Type": "application/json"})
        payload = json.loads(signed_destination.read_text(encoding="utf-8"))
    finally:
        signed_destination.unlink(missing_ok=True)
    signed_url = payload.get("data", {}).get("signedUrl")
    if not signed_url:
        raise AcquisitionFailure("Dataverse did not return a signed download URL.")
    # Dataverse serves stored files from its S3 bucket; only reviewed redirect hosts are followed.
    response = fetcher.request(signed_url, destination=destination, max_bytes=max_bytes,
                               redirect_hosts=set(access.get("redirect_hosts", [])))
    return url, response


def add_manual_artifact(root: DataRoot, connection: sqlite3.Connection, source_id: str, artifact_key: str,
                        path: Path, *, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Archive bytes the owner downloaded themselves (for example through a browser)."""
    spec = find_artifact_spec(source_id, artifact_key, manifest)
    if path.stat().st_size > spec["max_bytes"]:
        raise InvalidInput("File exceeds the manifest size limit.")
    temporary = staging_file(root)
    shutil.copyfile(path, temporary)
    artifact_id, created = archive_bytes(root, connection, temporary, format=spec["format"],
                                         mime_type=spec.get("mime_type"), original_filename=artifact_key)
    _record_retrieval(connection, source_id=source_id, artifact_key=artifact_key, artifact_id=artifact_id,
                      requested_url=f"file:///{path.name}", response=None, status="manual_import",
                      detail="Owner-supplied file; original URL retrieval not observed by this tool.")
    return {"artifact_id": artifact_id, "status": "new_artifact" if created else "unchanged"}


def latest_artifact(connection: sqlite3.Connection, source_id: str, artifact_key: str) -> str:
    row = _latest_retrieval(connection, source_id, artifact_key)
    if row is None:
        raise InvalidInput(f"No archived artifact for {source_id}/{artifact_key}; fetch it first.")
    return row["artifact_id"]
