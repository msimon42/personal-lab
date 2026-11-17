"""Census cartographic boundary shapefiles -> geographic reference and boundary features."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..core import DataRoot, ReviewRequired, canonical_json, sha256_text, stable_id
from ..pipeline import (Normalized, begin_run, complete_run, existing_result, geo_id, geo_version_id, unit_id)
from ..readers import bbox, read_shapefile_zip

ADAPTER_ID = "census-cartographic-boundary"
ADAPTER_VERSION = "1"
LAYERS = {
    "cb_2020_us_state_20m": {"level": "state", "fields": {"STATEFP", "STUSPS", "NAME", "GEOID"}},
    "cb_2020_us_county_20m": {"level": "county", "fields": {"STATEFP", "COUNTYFP", "GEOID", "NAME", "NAMELSAD"}},
}
VALID_FROM = "2020-01-01"
VINTAGE = "2020"


def _validate_geometry(feature: dict[str, Any]) -> list[str]:
    problems = []
    geometry = feature["geometry"]
    if geometry is None:
        return ["empty geometry"]
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for polygon in polygons:
        for ring in polygon:
            if len(ring) < 4 or ring[0] != ring[-1]:
                problems.append("ring is not closed or has fewer than four positions")
            if any(not (-180 <= x <= 180 and -90 <= y <= 90) for x, y in ring):
                problems.append("coordinate outside WGS84/NAD83 geographic range")
    return problems


def import_layer(root: DataRoot, connection: sqlite3.Connection, artifact_id: str, layer: str) -> dict:
    if layer not in LAYERS:
        raise ReviewRequired(f"Layer {layer} has no reviewed adapter mapping.")
    level = LAYERS[layer]["level"]
    context = begin_run(root, connection, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                        artifacts={"boundary": artifact_id}, mapping_version=f"census-{VINTAGE}",
                        mapping_hash=sha256_text(canonical_json(
                            {"level": level, "fields": sorted(LAYERS[layer]["fields"])})), config={"layer": layer})
    if context.existing:
        return existing_result(connection, context)
    features = read_shapefile_zip(context.paths["boundary"], layer)
    if not features:
        raise ReviewRequired("Boundary layer contains no features.")
    missing = LAYERS[layer]["fields"] - set(features[0]["properties"])
    projection = features[0]["projection"]
    if missing or "North_American_1983" not in projection:
        raise ReviewRequired(f"Boundary schema changed (missing fields {sorted(missing)}, projection {projection[:40]}).")
    boundary_set = stable_id("boundary_set", "census", VINTAGE, layer)
    normalized = Normalized()
    normalized.other.append((
        "INSERT OR IGNORE INTO boundary_set VALUES (?,?,?,?,?,?,?,?)",
        (boundary_set, "U.S. Census Bureau", VINTAGE, layer, "EPSG:4269 (NAD83 geographic)", artifact_id,
         "us-government-work", "cartographic_generalized_1:20m")))
    seen: set[str] = set()
    for feature in features:
        properties = feature["properties"]
        geoid = properties["GEOID"]
        if geoid in seen:
            raise ReviewRequired(f"Duplicate GEOID {geoid} in {layer}.")
        seen.add(geoid)
        if problems := _validate_geometry(feature):
            normalized.diagnostics.append({"geoid": geoid, "problems": sorted(set(problems))})
            continue
        if level == "state":
            natural, name, kind = f"state:{geoid}", properties["NAME"], "state"
            identifiers = [("state_fips", geoid), ("state_postal", properties["STUSPS"])]
        else:
            natural, name, kind = f"county:{geoid}", properties["NAMELSAD"], "county"
            identifiers = [("county_fips", geoid)]
        entity, version = geo_id(natural), geo_version_id(natural, VALID_FROM)
        normalized.other.append(("INSERT OR IGNORE INTO geographic_entity VALUES (?,?,?,?)",
                                 (entity, kind, name, natural)))
        normalized.other.append(("INSERT OR IGNORE INTO geographic_version VALUES (?,?,?,?,?,?,?)",
                                 (version, entity, name, VALID_FROM, None, artifact_id, f"census_vintage_{VINTAGE}")))
        for scheme, value in identifiers:
            normalized.other.append(("INSERT OR IGNORE INTO geographic_identifier VALUES (?,?,?,?)",
                                     (stable_id("geoident", scheme, value, version), version, scheme, value)))
        if level == "county":
            parent = geo_version_id(f"state:{properties['STATEFP']}", VALID_FROM)
            normalized.other.append(("INSERT OR IGNORE INTO geographic_relation VALUES (?,?,?,?,?,?,?)",
                                     (stable_id("georel", version, parent), version, parent, "within", VALID_FROM,
                                      None, artifact_id)))
        geometry_json = canonical_json(feature["geometry"])
        feature_id = stable_id("feature", boundary_set, geoid)
        normalized.other.append((
            "INSERT OR IGNORE INTO boundary_feature VALUES (?,?,?,?,?,?,?,?)",
            (feature_id, boundary_set, version, geoid, None, sha256_text(geometry_json),
             canonical_json(bbox(feature["geometry"])), geometry_json)))
        if level == "state":
            normalized.other.append((
                "INSERT OR IGNORE INTO reporting_unit VALUES (?,?,?,?,?,?,?,?)",
                (unit_id(natural), "state", version, None, name, geoid, natural, None)))
    normalized.accounting = {"features_read": len(features), "features_accepted": len(seen) - len(normalized.diagnostics),
                             "features_rejected": len(normalized.diagnostics)}
    return complete_run(connection, context, normalized)
