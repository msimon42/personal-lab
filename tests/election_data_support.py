"""Synthetic, offline election-data universe for contract tests.

Everything here is invented except the verbatim CC0 MIT excerpt for Alamance County, NC
(tests/fixtures/election_data/mit-nc-alamance-2024.tab). Geometry is a grid of squares.
Synthetic fixtures establish synthetic contracts only.
"""
from __future__ import annotations

import io
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "election_data"
HEADER = ["state", "county_name", "year", "state_po", "county_fips", "office", "candidate", "party",
          "candidatevotes", "totalvotes", "version", "mode"]
STATES = [
    ("AL", "01", "Alabama"), ("AK", "02", "Alaska"), ("AZ", "04", "Arizona"), ("AR", "05", "Arkansas"),
    ("CA", "06", "California"), ("CO", "08", "Colorado"), ("CT", "09", "Connecticut"), ("DE", "10", "Delaware"),
    ("DC", "11", "District of Columbia"), ("FL", "12", "Florida"), ("GA", "13", "Georgia"), ("HI", "15", "Hawaii"),
    ("ID", "16", "Idaho"), ("IL", "17", "Illinois"), ("IN", "18", "Indiana"), ("IA", "19", "Iowa"),
    ("KS", "20", "Kansas"), ("KY", "21", "Kentucky"), ("LA", "22", "Louisiana"), ("ME", "23", "Maine"),
    ("MD", "24", "Maryland"), ("MA", "25", "Massachusetts"), ("MI", "26", "Michigan"), ("MN", "27", "Minnesota"),
    ("MS", "28", "Mississippi"), ("MO", "29", "Missouri"), ("MT", "30", "Montana"), ("NE", "31", "Nebraska"),
    ("NV", "32", "Nevada"), ("NH", "33", "New Hampshire"), ("NJ", "34", "New Jersey"), ("NM", "35", "New Mexico"),
    ("NY", "36", "New York"), ("NC", "37", "North Carolina"), ("ND", "38", "North Dakota"), ("OH", "39", "Ohio"),
    ("OK", "40", "Oklahoma"), ("OR", "41", "Oregon"), ("PA", "42", "Pennsylvania"), ("RI", "44", "Rhode Island"),
    ("SC", "45", "South Carolina"), ("SD", "46", "South Dakota"), ("TN", "47", "Tennessee"), ("TX", "48", "Texas"),
    ("UT", "49", "Utah"), ("VT", "50", "Vermont"), ("VA", "51", "Virginia"), ("WA", "53", "Washington"),
    ("WV", "54", "West Virginia"), ("WI", "55", "Wisconsin"), ("WY", "56", "Wyoming"), ("PR", "72", "Puerto Rico"),
]
CANDIDATES = [("KAMALA D HARRIS", "DEMOCRAT"), ("DONALD J TRUMP", "REPUBLICAN"), ("CHASE OLIVER", "LIBERTARIAN"),
              ("OTHER", "OTHER")]
ELECTORS = {"ME": 4, "NE": 5}


# --- shapefile writer ---------------------------------------------------------------------------
def _square(x: float, y: float, width: float, height: float) -> list[tuple[float, float]]:
    # ESRI exterior rings are clockwise.
    return [(x, y), (x, y + height), (x + width, y + height), (x + width, y), (x, y)]


def _shp_record(number: int, rings: list[list[tuple[float, float]]]) -> bytes:
    points = [point for ring in rings for point in ring]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    parts, offset = [], 0
    for ring in rings:
        parts.append(offset)
        offset += len(ring)
    content = struct.pack("<i4d2i", 5, min(xs), min(ys), max(xs), max(ys), len(rings), len(points))
    content += struct.pack(f"<{len(parts)}i", *parts)
    content += b"".join(struct.pack("<2d", *point) for point in points)
    return struct.pack(">2i", number, len(content) // 2) + content


def shapefile_zip(layer: str, records: list[tuple[dict[str, str], list[list[tuple[float, float]]]]]) -> bytes:
    body = b"".join(_shp_record(index + 1, rings) for index, (_, rings) in enumerate(records))
    header = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, (100 + len(body)) // 2) + struct.pack("<2i4d4d", 1000, 5,
                                                                                         -180, -90, 180, 90, 0, 0, 0, 0)
    fields = list(records[0][0])
    widths = {name: max(len(name), *(len(str(row[0][name])) for row in records)) + 2 for name in fields}
    record_length = 1 + sum(widths.values())
    dbf = struct.pack("<4BIHH20x", 3, 126, 1, 1, len(records), 32 + 32 * len(fields) + 1, record_length)
    for name in fields:
        dbf += name.encode("ascii").ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([widths[name], 0]) + b"\0" * 14
    dbf += b"\r"
    for properties, _ in records:
        dbf += b" " + b"".join(str(properties[name]).encode("utf-8").ljust(widths[name]) for name in fields)
    dbf += b"\x1a"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{layer}.shp", header + body)
        archive.writestr(f"{layer}.dbf", dbf)
        archive.writestr(f"{layer}.prj", 'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983"]]')
        archive.writestr(f"{layer}.cpg", "UTF-8")
    return buffer.getvalue()


# --- xlsx writer (inline strings) ---------------------------------------------------------------
def _column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def xlsx(sheet: str, rows: list[list[str | int | None]]) -> bytes:
    cells = []
    for row_number, row in enumerate(rows, start=1):
        values = []
        for index, value in enumerate(row):
            reference = f"{_column(index)}{row_number}"
            if value is None:
                continue
            if isinstance(value, int):
                values.append(f'<c r="{reference}"><v>{value}</v></c>')
            else:
                values.append(f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>')
        cells.append(f'<row r="{row_number}">{"".join(values)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{relationship}"><sheets>'
                                            f'<sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{main}"><sheetData>{"".join(cells)}'
                                                     "</sheetData></worksheet>")
    return buffer.getvalue()


# --- universe -------------------------------------------------------------------------------------
@dataclass
class Universe:
    mit_rows: list[list[str]] = field(default_factory=list)
    counties: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # postal -> [(fips, name)]
    totals: dict[str, dict[str, int]] = field(default_factory=dict)  # postal -> candidate -> votes
    fec_adjust: dict[tuple[str, str], int] = field(default_factory=dict)

    def county_rows(self, postal: str, name: str, fips: str, votes: dict[str, int | None], *, mode: str = "TOTAL",
                    record_totals: bool = True, totalvotes: int | None = None) -> None:
        state = next(row[2] for row in STATES if row[0] == postal)
        total = totalvotes if totalvotes is not None else sum(value or 0 for value in votes.values())
        for candidate, party in CANDIDATES:
            if candidate not in votes:
                continue
            value = votes[candidate]
            self.mit_rows.append([state.upper(), name, "2024", postal, str(int(fips)), "US PRESIDENT", candidate,
                                  party, "" if value is None else str(value), str(total), "20260225", mode])
            if record_totals and value is not None:
                self.totals.setdefault(postal, {}).setdefault(candidate, 0)
                self.totals[postal][candidate] += value


def _votes(seed: int, lean: int) -> dict[str, int]:
    harris = 1000 + seed * 37
    trump = harris + lean
    return {"KAMALA D HARRIS": harris, "DONALD J TRUMP": trump, "CHASE OLIVER": 20 + seed, "OTHER": 10 + seed}


def build_universe() -> Universe:
    """Default universe; every state reconciles except PA (FEC adds 100 Harris votes)."""
    universe = Universe()
    lean = {"ME": -800, "NE": 900, "AK": 700, "CA": -900, "NY": -700}
    for index, (postal, fips, name) in enumerate(STATES):
        if postal == "PR":
            continue
        counties = [(f"{fips}001", f"FIRST {postal}"), (f"{fips}003", f"SECOND {postal}")]
        if postal == "AK":
            counties = [("02013", "Aleutians East Borough"), ("02016", "Aleutians West Census Area")]
        if postal == "MO":
            counties.append(("29095", "JACKSON"))
        if postal == "SD":
            counties = [("46001", "AURORA"), ("46102", "OGLALA LAKOTA")]
        universe.counties[postal] = counties
        state_lean = lean.get(postal, 300 if index % 2 else -300)
        if postal == "AK":
            universe.county_rows("AK", "DISTRICT 01", "02001", _votes(1, 700))
            universe.county_rows("AK", "DISTRICT 02", "02002", _votes(2, 700))
            universe.county_rows("AK", "DISTRICT 99", "02099", _votes(3, 700))
            continue
        if postal == "NC":
            real = (FIXTURES / "mit-nc-alamance-2024.tab").read_text(encoding="utf-8").splitlines()[1:]
            for line in real:
                fields = [value.strip('"') for value in line.split("\t")]
                universe.mit_rows.append(fields)
                universe.totals.setdefault("NC", {}).setdefault(fields[6], 0)
                universe.totals["NC"][fields[6]] += int(fields[8])
            universe.county_rows("NC", "SECOND NC", "37003", _votes(5, 300), mode="")
            continue
        if postal == "SD":
            universe.county_rows("SD", "AURORA", "46001", _votes(4, 200))
            universe.county_rows("SD", "OGLALA LAKOTA", "46113", _votes(6, -100))  # pre-2015 code
            continue
        for seed, (county_fips, county_name) in enumerate(counties):
            votes = _votes(seed + index, state_lean)
            if postal == "MO" and county_fips == "29095":
                universe.county_rows("MO", "JACKSON", "29095", votes)
                universe.county_rows("MO", "KANSAS CITY", "36000", _votes(9, -500))
                continue
            if postal == "NM" and seed == 0:
                votes = {**votes, "CHASE OLIVER": None}
            if postal == "WY":
                universe.county_rows(postal, county_name, county_fips, votes, mode="")
                continue
            if postal == "OR" and seed == 0:
                # Modes without TOTAL and without a reviewed decision: must be withheld.
                half = {key: value // 2 for key, value in votes.items()}
                universe.county_rows(postal, county_name, county_fips, half, mode="ELECTION DAY")
                universe.county_rows(postal, county_name, county_fips,
                                     {key: votes[key] - half[key] for key in votes}, mode="MAIL")
                continue
            universe.county_rows(postal, county_name, county_fips, votes)
            if postal == "AZ" and seed == 0:
                universe.mit_rows.append(["ARIZONA", county_name, "2024", "AZ", str(int(county_fips)), "US PRESIDENT",
                                          "UNDERVOTES", "OTHER", "12", str(sum(votes.values())), "20260225", "TOTAL"])
    universe.fec_adjust[("PA", "KAMALA D HARRIS")] = 100
    return universe


def mit_tsv(universe: Universe, extra_years: bool = True) -> bytes:
    lines = ["\t".join(HEADER)]
    if extra_years:
        lines.append("\t".join(["ALABAMA", "FIRST AL", "2020", "AL", "1001", "US PRESIDENT", "JOSEPH R BIDEN JR",
                                "DEMOCRAT", "5", "10", "20220315", "TOTAL"]))
    for row in universe.mit_rows:
        quoted = [value if index in (2, 4, 8, 9, 10) else f'"{value}"' for index, value in enumerate(row)]
        lines.append("\t".join(quoted))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _winner(universe: Universe, postal: str) -> str:
    totals = universe.totals[postal]
    return "TRUMP" if totals["DONALD J TRUMP"] > totals["KAMALA D HARRIS"] else "HARRIS"


def fec_workbook(universe: Universe) -> bytes:
    header = ["STATE", "ELECTORAL VOTES", "ELECTORAL VOTE: TRUMP (R)", "ELECTORAL VOTE: HARRIS (D)", "HARRIS",
              "OLIVER", "STEIN", "TRUMP", "WRITE-INS (SCATTERED)", "TOTAL VOTES"]
    rows: list[list] = [header]
    for postal, _, _ in STATES:
        if postal == "PR":
            continue
        totals = universe.totals[postal]
        harris = totals["KAMALA D HARRIS"] + universe.fec_adjust.get((postal, "KAMALA D HARRIS"), 0)
        trump, oliver = totals["DONALD J TRUMP"], totals.get("CHASE OLIVER", 0)
        other = totals.get("OTHER", 0)
        electors = ELECTORS.get(postal, 3)
        if postal == "ME":
            trump_ev, harris_ev = 1, 3
        elif postal == "NE":
            trump_ev, harris_ev = 4, 1
        else:
            trump_ev, harris_ev = (electors, None) if _winner(universe, postal) == "TRUMP" else (None, electors)
        rows.append([postal, electors, trump_ev, harris_ev, harris, oliver or None, other or None, trump, None,
                     harris + trump + oliver + other])
    rows.append(["Total:", sum(ELECTORS.get(p, 3) for p, _, _ in STATES if p != "PR")])
    rows.append([None, "*Source: synthetic fixture"])
    return xlsx("OFFICIAL 2024 PRES GE RESULTS", rows)


def nara_page(universe: Universe) -> bytes:
    rows = []
    totals = [0, 0, 0, 0, 0]
    for postal, _, name in STATES:
        if postal == "PR":
            continue
        electors = ELECTORS.get(postal, 3)
        if postal == "ME":
            harris, trump = 3, 1
        elif postal == "NE":
            harris, trump = 1, 4
        elif _winner(universe, postal) == "TRUMP":
            harris, trump = 0, electors
        else:
            harris, trump = electors, 0
        cells = [electors, harris, trump, harris, trump]
        totals = [a + b for a, b in zip(totals, cells)]
        label = f"{name}{2 if postal == 'ME' else 3 if postal == 'NE' else ''}"
        rows.append(f"<tr><td>{label}</td>" + "".join(f"<td>{value or '-'}</td>" for value in cells) + "</tr>")
    rows.append("<tr><td>Total</td>" + "".join(f"<td>{value}</td>" for value in totals) + "</tr>")
    rows.append("<tr><td>Notes</td></tr>")
    return ("<html><body><table><tr><td>summary</td></tr></table><table>"
            "<tr><th>State</th><th>Number of Electoral Votes for Each State</th><th>For President</th>"
            "<th>For Vice-President</th></tr><tr><th>Kamala D. Harris, of California</th>"
            "<th>Donald J. Trump, of Florida</th><th>Tim Walz,&nbsp;of Minnesota</th><th>JD Vance,&nbsp;of Ohio</th></tr>"
            + "".join(rows) + "</table></body></html>").encode("utf-8")


def boundary_zips(universe: Universe) -> tuple[bytes, bytes]:
    state_records, county_records = [], []
    for index, (postal, fips, name) in enumerate(STATES):
        x, y = -170 + (index % 10) * 10, 20 + (index // 10) * 8
        state_records.append(({"STATEFP": fips, "STATENS": "0", "AFFGEOID": f"0400000US{fips}", "GEOID": fips,
                               "STUSPS": postal, "NAME": name, "LSAD": "00"}, [_square(x, y, 6, 6)]))
        counties = universe.counties.get(postal, [(f"{fips}001", f"Only {postal}")])
        width = 6 / len(counties)
        for offset, (county_fips, county_name) in enumerate(counties):
            rings = [_square(x + offset * width, y, width, 6)]
            if postal == "HI" and offset == 0:
                # A hole (counter-clockwise ring) inside the first county exercises orientation handling.
                rings.append(list(reversed(_square(x + 0.5, y + 0.5, 1, 1))))
            county_records.append(({"STATEFP": fips, "COUNTYFP": county_fips[2:], "GEOID": county_fips,
                                    "NAME": county_name.title(), "NAMELSAD": f"{county_name.title()} County",
                                    "STUSPS": postal}, rings))
    return (shapefile_zip("cb_2020_us_state_20m", state_records),
            shapefile_zip("cb_2020_us_county_20m", county_records))


def write_sources(directory: Path, universe: Universe | None = None) -> dict[str, Path]:
    """Write synthetic source files; returns adapter name -> path."""
    universe = universe or build_universe()
    directory.mkdir(parents=True, exist_ok=True)
    state_zip, county_zip = boundary_zips(universe)
    files = {"census-state-2020": ("state.zip", state_zip), "census-county-2020": ("county.zip", county_zip),
             "fec-president-2024": ("fec.xlsx", fec_workbook(universe)),
             "mit-county-president-2024": ("mit.tab", mit_tsv(universe)),
             "nara-electoral-2024": ("nara.html", nara_page(universe))}
    paths = {}
    for adapter, (name, payload) in files.items():
        path = directory / name
        path.write_bytes(payload)
        paths[adapter] = path
    return paths


def load_universe(root_dir: Path, sources: dict[str, Path]):
    """Initialize a data root, archive the synthetic files and import them in order."""
    from contextlib import closing

    from election_data.acquire import add_manual_artifact, register_sources
    from election_data.core import DataRoot, connect, migrate, writer_lock
    from election_data.importer import ADAPTERS, run_adapter
    from election_data.pipeline import ensure_reference, load_curation

    root = DataRoot.open(root_dir, create=True)
    results = {}
    with closing(connect(root.database)) as connection, writer_lock(root):
        migrate(connection)
        register_sources(connection)
        ensure_reference(connection, load_curation()[0])
        for name in sorted(ADAPTERS, key=lambda key: ADAPTERS[key].order):
            entry = ADAPTERS[name]
            artifact = add_manual_artifact(root, connection, entry.source_id, entry.artifact_key, sources[name])
            results[name] = run_adapter(root, connection, name, artifact["artifact_id"])
    return root, results
