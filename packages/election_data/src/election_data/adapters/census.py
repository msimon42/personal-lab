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


# --- Congressional districts ----------------------------------------------------------------------
CENSUS_CD = {113: "cb_2013_us_cd113_20m", 114: "cb_2014_us_cd114_20m", 115: "cb_2016_us_cd115_20m",
             116: "cb_2018_us_cd116_20m", 118: "cb_2022_us_cd118_20m", 119: "cb_2024_us_cd119_20m"}
UCLA_CONGRESSES = range(95, 113)


def congress_start(congress: int) -> str:
    year = 1789 + 2 * (congress - 1)
    return f"{year}-01-03" if year >= 1935 else f"{year}-03-04"


def district_boundary_set(congress: int) -> str:
    return stable_id("boundary_set", "congressional-districts", congress)


def import_districts(root: DataRoot, connection: sqlite3.Connection, artifact_id: str, congress: int) -> dict:
    """Import one Congress's districts from Census (113+) or UCLA (95-112) files."""
    from ..federal import decision, states
    from ..readers import quantize_geometry

    ucla = congress in UCLA_CONGRESSES
    if not ucla and congress not in CENSUS_CD:
        raise ReviewRequired(f"No reviewed district boundary source for the {congress}th Congress.")
    layer = f"districts{congress:03d}" if ucla else CENSUS_CD[congress]
    rules = decision("dec-fed-district-boundaries")["scope"]
    context = begin_run(root, connection, adapter_id="congressional-district-boundaries", adapter_version="2",
                        artifacts={"boundary": artifact_id}, mapping_version="district-boundaries-v1",
                        mapping_hash=sha256_text(canonical_json(rules)), config={"congress": congress})
    if context.existing:
        return existing_result(connection, context)
    features = read_shapefile_zip(context.paths["boundary"], layer)
    if not features or "North_American_1983" not in features[0]["projection"]:
        raise ReviewRequired("District boundary layer is empty or not in NAD83 geographic coordinates.")
    field = f"CD{congress}FP"
    required = {"STATENAME", "DISTRICT", "STARTCONG", "ENDCONG", "ID"} if ucla else {"STATEFP", field, "GEOID"}
    if missing := required - set(features[0]["properties"]):
        raise ReviewRequired(f"District boundary schema changed: missing fields {sorted(missing)}.")
    by_fips = {row["fips"]: postal for postal, row in states().items()}
    by_name = {row["name"].upper(): postal for postal, row in states().items()}
    boundary_set = district_boundary_set(congress)
    publisher = "Lewis, DeVine, Pitcher and Martis (UCLA)" if ucla else "U.S. Census Bureau"
    normalized = Normalized()
    normalized.other.append(("INSERT OR IGNORE INTO boundary_set VALUES (?,?,?,?,?,?,?,?)", (
        boundary_set, publisher, f"congress-{congress}", layer, "EPSG:4269 (NAD83 geographic)", artifact_id,
        "mit-license-ucla-cdmaps" if ucla else "us-government-work",
        f"grid_quantized_{rules['quantization_decimals']}_decimals" if ucla else "cartographic_generalized_1:20m")))
    fallback = rules["congress_117_fallback"]
    accounting = {"features_read": len(features), "features_accepted": 0, "outside_scope": 0, "rejected": 0,
                  "later_plan_same_congress": 0}
    seen: set[str] = set()
    if ucla:
        # A district redrawn during a Congress appears twice; the plan already in force when the
        # Congress was elected (the earlier STARTCONG) is the one its election used.
        features = sorted(features, key=lambda item: (item["properties"]["STATENAME"],
                                                      float(item["properties"]["DISTRICT"]),
                                                      float(item["properties"]["STARTCONG"]), item["record"]))
    for feature in features:
        properties = feature["properties"]
        if ucla:
            postal = by_name.get(properties["STATENAME"].upper())
            number = int(float(properties["DISTRICT"]))
            code = None if number < 0 else f"{number:02d}"
            natural = f"cd:ucla:{properties['ID']}"
            start, end = int(float(properties["STARTCONG"])), int(float(properties["ENDCONG"]))
        else:
            postal = by_fips.get(properties["STATEFP"])
            code = properties[field] if properties[field].isdigit() else None
            natural = f"cd:census:{congress}:{properties['GEOID']}"
            start = end = congress
        if postal is None or code is None:
            accounting["outside_scope"] += 1  # territories, undefined areas, general-ticket shapes
            continue
        geometry = feature["geometry"]
        if geometry is not None and ucla:
            geometry = quantize_geometry(geometry, rules["quantization_decimals"])
        key = f"{postal}:{code}"
        if key in seen:
            accounting["later_plan_same_congress"] += 1
            normalized.diagnostics.append({"district": key, "note": "later plan within the same Congress not used"})
            continue
        if geometry is None or _validate_geometry({"geometry": geometry}):
            accounting["rejected"] += 1
            normalized.diagnostics.append({"district": key, "problem": "empty, invalid or collapsed geometry"})
            continue
        seen.add(key)
        entity, version = geo_id(natural), geo_version_id(natural, congress_start(start))
        name = f"{states()[postal]['name']} " + ("at-large" if code == "00" else "delegate district" if code == "98"
                                                 else f"district {int(code)}")
        normalized.other.append(("INSERT OR IGNORE INTO geographic_entity VALUES (?,?,?,?)",
                                 (entity, "congressional_district", name, natural)))
        normalized.other.append(("INSERT OR IGNORE INTO geographic_version VALUES (?,?,?,?,?,?,?)", (
            version, entity, name, congress_start(start), congress_start(end + 1), artifact_id,
            f"congress_{start}_to_{end}")))
        congresses = [congress]
        if congress == fallback["use"] and postal not in fallback["except_states"]:
            congresses.append(117)
        for number in congresses:
            value = f"{number}:{postal}:{code}"
            normalized.other.append(("INSERT OR IGNORE INTO geographic_identifier VALUES (?,?,?,?)",
                                     (stable_id("geoident", "cd_congress", value, version), version, "cd_congress", value)))
        geometry_json = canonical_json(geometry)
        normalized.other.append(("INSERT OR IGNORE INTO boundary_feature VALUES (?,?,?,?,?,?,?,?)", (
            stable_id("feature", boundary_set, key), boundary_set, version, key, None, sha256_text(geometry_json),
            canonical_json(bbox(geometry)), geometry_json)))
        accounting["features_accepted"] += 1
    normalized.accounting = accounting
    return complete_run(connection, context, normalized)
