"""Bounded standard-library readers for untrusted downloaded formats.

Downloaded files are data. Archive members are read by exact name from a size-
checked zip; nothing is extracted to disk, no macro or script is executed, and
decompressed sizes and compression ratios are capped.
"""
from __future__ import annotations

import html
import html.parser
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .core import InvalidInput

MAX_MEMBER_BYTES = 200_000_000
MAX_MEMBERS = 200
MAX_RATIO = 200


def open_bounded_zip(path: Path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        raise InvalidInput("Archive contains too many members.")
    for member in members:
        name = member.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
            raise InvalidInput(f"Archive member path is unsafe: {name!r}")
        if member.file_size > MAX_MEMBER_BYTES:
            raise InvalidInput(f"Archive member {name!r} is too large.")
        if member.compress_size and member.file_size / member.compress_size > MAX_RATIO:
            raise InvalidInput(f"Archive member {name!r} has a suspicious compression ratio.")
    return archive


# --- XLSX -----------------------------------------------------------------------------------------
_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _column(reference: str) -> str:
    return "".join(character for character in reference if character.isalpha())


def read_xlsx(path: Path) -> list[dict[str, Any]]:
    """Return ``[{"sheet": name, "row": n, "cells": {"A": value, ...}}]`` with raw text values."""
    with open_bounded_zip(path) as archive:
        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for item in ET.fromstring(archive.read("xl/sharedStrings.xml")):
                shared.append("".join(text.text or "" for text in item.iter(f"{_M}t")))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target") for rel in relationships}
        rows = []
        for sheet in workbook.find(f"{_M}sheets"):
            target = targets[sheet.get(f"{_R}id")].lstrip("/")
            member = target if target.startswith("xl/") else f"xl/{target}"
            if member not in names:
                raise InvalidInput(f"Workbook references missing sheet {member}.")
            for row in ET.fromstring(archive.read(member)).iter(f"{_M}row"):
                cells = {}
                for cell in row.findall(f"{_M}c"):
                    kind = cell.get("t")
                    value_node = cell.find(f"{_M}v")
                    value = None if value_node is None else value_node.text
                    if kind == "s" and value is not None:
                        value = shared[int(value)]
                    elif kind == "inlineStr":
                        value = "".join(text.text or "" for text in cell.iter(f"{_M}t"))
                    if value is not None:
                        cells[_column(cell.get("r"))] = value
                rows.append({"sheet": sheet.get("name"), "row": int(row.get("r")), "cells": cells})
        return rows


# --- HTML tables ----------------------------------------------------------------------------------
class _TableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.depth = 0
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            self.tables.append([])
        elif tag == "tr" and self.depth:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.tables[-1].append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def read_html_tables(path: Path, max_bytes: int = 10_000_000) -> list[list[list[str]]]:
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise InvalidInput("HTML document is too large.")
    parser = _TableParser()
    parser.feed(raw.decode("utf-8", "replace"))
    return parser.tables


# --- Shapefiles -----------------------------------------------------------------------------------
def _signed_area(ring: list[tuple[float, float]]) -> float:
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2


def _contains(ring: list[tuple[float, float]], point: tuple[float, float]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _polygon_geometry(rings: list[list[tuple[float, float]]], precision: int) -> dict[str, Any]:
    """ESRI rings (clockwise exterior) to RFC 7946 (counter-clockwise exterior) polygons."""
    exteriors, holes = [], []
    for ring in rings:
        (exteriors if _signed_area(ring) < 0 else holes).append(ring)
    if not exteriors:
        raise InvalidInput("Polygon record has no exterior ring.")
    polygons = [[exterior] for exterior in exteriors]
    for hole in holes:
        owner = next((polygon for polygon in polygons if _contains(polygon[0], hole[0])), None)
        if owner is None:
            raise InvalidInput("Polygon hole is not inside any exterior ring.")
        owner.append(hole)

    def emit(ring, exterior):
        oriented = list(reversed(ring)) if exterior else ring
        return [[round(x, precision), round(y, precision)] for x, y in oriented]

    coordinates = [[emit(ring, index == 0) for index, ring in enumerate(polygon)] for polygon in polygons]
    if len(coordinates) == 1:
        return {"type": "Polygon", "coordinates": coordinates[0]}
    return {"type": "MultiPolygon", "coordinates": coordinates}


def _read_shp(raw: bytes, precision: int) -> Iterator[dict[str, Any] | None]:
    if len(raw) < 100 or struct.unpack(">i", raw[:4])[0] != 9994:
        raise InvalidInput("Not an ESRI shapefile.")
    shape_type = struct.unpack("<i", raw[32:36])[0]
    if shape_type != 5:
        raise InvalidInput(f"Only polygon shapefiles are supported (type {shape_type}).")
    offset = 100
    while offset < len(raw):
        _, length_words = struct.unpack(">ii", raw[offset:offset + 8])
        content = raw[offset + 8: offset + 8 + length_words * 2]
        offset += 8 + length_words * 2
        record_type = struct.unpack("<i", content[:4])[0]
        if record_type == 0:
            yield None
            continue
        if record_type != 5:
            raise InvalidInput(f"Unexpected shape record type {record_type}.")
        parts_count, points_count = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack(f"<{parts_count}i", content[44:44 + 4 * parts_count]))
        start = 44 + 4 * parts_count
        values = struct.unpack(f"<{points_count * 2}d", content[start:start + 16 * points_count])
        points = list(zip(values[0::2], values[1::2]))
        rings = [points[begin:end] for begin, end in zip(parts, parts[1:] + [points_count])]
        yield _polygon_geometry(rings, precision)


def _read_dbf(raw: bytes, encoding: str) -> list[dict[str, str]]:
    count, header_length, record_length = struct.unpack("<IHH", raw[4:12])
    fields = []
    for offset in range(32, header_length - 1, 32):
        descriptor = raw[offset:offset + 32]
        if descriptor[0] == 0x0D:
            break
        fields.append((descriptor[:11].split(b"\0")[0].decode("ascii"), descriptor[16]))
    records = []
    for index in range(count):
        start = header_length + index * record_length
        record = raw[start:start + record_length]
        if record[:1] == b"*":
            records.append({})
            continue
        position, values = 1, {}
        for name, length in fields:
            values[name] = record[position:position + length].decode(encoding).strip()
            position += length
        records.append(values)
    return records


def read_shapefile_zip(path: Path, layer: str, *, precision: int = 6) -> list[dict[str, Any]]:
    """Return GeoJSON-like features ``{"properties", "geometry", "record"}`` for one layer."""
    with open_bounded_zip(path) as archive:
        names = set(archive.namelist())
        for extension in ("shp", "dbf"):
            if f"{layer}.{extension}" not in names:
                raise InvalidInput(f"Shapefile archive lacks {layer}.{extension}.")
        encoding = "utf-8"
        if f"{layer}.cpg" in names:
            declared = archive.read(f"{layer}.cpg").decode("ascii", "replace").strip().lower()
            encoding = {"utf-8": "utf-8", "utf8": "utf-8", "1252": "cp1252"}.get(declared, "latin-1")
        projection = archive.read(f"{layer}.prj").decode("ascii", "replace") if f"{layer}.prj" in names else ""
        geometries = list(_read_shp(archive.read(f"{layer}.shp"), precision))
        attributes = _read_dbf(archive.read(f"{layer}.dbf"), encoding)
    if len(geometries) != len(attributes):
        raise InvalidInput("Shapefile geometry and attribute record counts differ.")
    return [{"record": index, "properties": properties, "geometry": geometry, "projection": projection}
            for index, (properties, geometry) in enumerate(zip(attributes, geometries))]


def bbox(geometry: dict[str, Any]) -> list[float]:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    xs = [point[0] for polygon in polygons for ring in polygon for point in ring]
    ys = [point[1] for polygon in polygons for ring in polygon for point in ring]
    return [min(xs), min(ys), max(xs), max(ys)]
