"""Federal reference tables, curated tickets and shared contest/candidacy registration.

Everything here is deterministic from two committed files: ``reference-federal.json``
(derived from hash-pinned Census and MIT artifacts) and ``curation-federal.json``
(reviewed decisions). Nothing is inferred from candidate display names alone.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from functools import lru_cache
from importlib import resources
from typing import Any

from .core import InvalidInput, canonical_json, sha256_text, stable_id
from .pipeline import (candidacy_id, contest_id, election_id, geo_id, office_id, option_id, party_id, person_id,
                       state_versions, unit_id)

OFFICES = {
    "president": ("President of the United States", "federal", "electoral_college"),
    "senate": ("United States Senator", "federal", "popular_vote"),
    "house": ("United States Representative", "federal", "popular_vote"),
    "house_delegate": ("Delegate to the House of Representatives (non-voting)", "federal", "popular_vote"),
}
FIRST_MODERN_CYCLE = 1976
SENATE_CLASS_BY_REMAINDER = {2: 1, 4: 2, 0: 3}
RANKED_CHOICE = {  # first cycle in which the office used ranked-choice tabulation
    "ME": {"house": 2018, "senate": 2018, "president": 2020},
    "AK": {"house": 2022, "senate": 2022, "president": 2024},
}
MAJORITY_RUNOFF = {"GA": 1976, "LA": 1978}


@lru_cache(maxsize=None)
def _load(name: str) -> tuple[dict[str, Any], str]:
    text = resources.files("election_data.manifests").joinpath(name).read_text(encoding="utf-8")
    value = json.loads(text)
    return value, sha256_text(canonical_json(value))


def load_reference() -> dict[str, Any]:
    return _load("reference-federal.json")[0]


def load_federal() -> tuple[dict[str, Any], str]:
    curation, digest = _load("curation-federal.json")
    return curation, sha256_text(digest + _load("reference-federal.json")[1])


def decision(decision_id: str) -> dict[str, Any]:
    return next(item for item in load_federal()[0]["decisions"] if item["decision_id"] == decision_id)


def states() -> dict[str, dict[str, str]]:
    return {row["postal"]: row for row in load_reference()["states"]}


def general_election_date(year: int) -> date:
    """Tuesday after the first Monday in November (federal election day since 1845)."""
    first = date(year, 11, 1)
    monday = first + timedelta(days=(7 - first.weekday()) % 7)
    return monday + timedelta(days=1)


def congress_for_election(year: int) -> int:
    return (year - 1788) // 2 + 1


def house_seats(postal: str, year: int) -> int:
    census = str(((year - 2) // 10) * 10)
    return load_reference()["house_seats_by_census"].get(postal, {}).get(census, 0)


def voting_system(office: str, postal: str, year: int) -> str:
    if year >= RANKED_CHOICE.get(postal, {}).get(office, 9999):
        return "ranked_choice"
    if office in ("senate", "house") and year >= MAJORITY_RUNOFF.get(postal, 9999):
        return "majority_runoff"
    return "plurality"


def president_seats(postal: str, year: int) -> list[str]:
    districts = house_seats(postal, year) if (postal, year >= 1972) == ("ME", True) or (postal, year >= 1992) == ("NE", True) else 0
    if districts:
        return [f"president:{postal}:at-large"] + [f"president:{postal}:cd{number}" for number in range(1, districts + 1)]
    return [f"president:{postal}"]


def statewide_president_seat(postal: str, year: int) -> str:
    return president_seats(postal, year)[0]


def house_seat(postal: str, district: int, year: int) -> str:
    return f"house:{postal}:{0 if house_seats(postal, year) == 1 else district:02d}"


def expected_contests(office: str, year: int) -> list[str]:
    """Seat keys of regular general contests expected in a cycle (1976 onward)."""
    if year < FIRST_MODERN_CYCLE or year % 2:
        return []
    reference = load_reference()
    if office == "president":
        return [seat for postal in states() for seat in president_seats(postal, year)] if year % 4 == 0 else []
    if office == "senate":
        number = SENATE_CLASS_BY_REMAINDER[year % 6]
        return [f"senate:{postal}:c{number}" for postal, classes in reference["senate_classes"].items() if number in classes]
    if office == "house":
        return [house_seat(postal, district, year) for postal in reference["house_seats_by_census"]
                for district in range(1, house_seats(postal, year) + 1)]
    return []


def senate_seat(postal: str, year: int, special: bool) -> str:
    classes = load_reference()["senate_classes"][postal]
    regular = SENATE_CLASS_BY_REMAINDER.get(year % 6) if year % 2 == 0 else None
    if not special:
        if regular not in classes:
            raise InvalidInput(f"{postal} has no class {regular} Senate seat regularly elected in {year}.")
        return f"senate:{postal}:c{regular}"
    number = decision("dec-fed-senate-special-seats")["scope"]["special_seat_classes"].get(f"{postal}:{year}")
    return f"senate:{postal}:c{number}" if number in classes else f"senate:{postal}:class-unresolved"


# --- labels ----------------------------------------------------------------------------------------
def normalize_name(label: str) -> str:
    """'LAST, FIRST M.' and 'FIRST M LAST' normalize to the same key; punctuation is dropped."""
    text = label.upper().replace('"', " ")
    if text.count(",") == 1 and not re.search(r",\s*(JR|SR|II|III|IV)\b\.?$", text):
        last, first = text.split(",")
        text = f"{first} {last}"
    return re.sub(r"[^A-Z0-9]+", "-", text).strip("-").lower()


def classify_label(label: str, writein: bool = False) -> tuple[str, str]:
    """Return ('metric', code), ('aggregate', key) or ('candidate', '')."""
    scope = decision("dec-fed-label-classes")["scope"]
    text = " ".join(label.upper().split())
    if text in scope["metrics"]:
        return "metric", scope["metrics"][text]
    if text in scope["aggregate_write_in"]:
        return "aggregate", "aggregate-write-in" if (text or writein) else "aggregate-unnamed"
    if text in scope["aggregate_other"]:
        return "aggregate", "aggregate-other"
    if text in scope["none_of_these"]:
        return "aggregate", "none-of-these-candidates"
    return "candidate", ""


def match_ticket(year: int, label: str, *, role: str = "president") -> str | None:
    """Curated ticket member whose surname and a given-name token both appear in the label."""
    curation = load_federal()[0]
    tokens = set(re.findall(r"[A-Z]+", label.upper().replace(".", "")))
    people = {person["key"]: person for person in curation["persons"]}
    for ticket in curation["tickets"].get(str(year), []):
        person = people[ticket[role]]
        if person["surname"] in tokens and tokens & set(person["given"]):
            return person["key"]
    return None


def stage_for(office: str, postal: str, year: int, source_stage: str, runoff: bool) -> str:
    overrides = decision("dec-fed-stage-overrides")["scope"].get(office, {}).get(postal, {}).get(str(year), {})
    stage = source_stage.strip().lower()
    if stage in overrides:
        return overrides[stage]
    if runoff or "runoff" in stage:
        return "runoff"
    if stage in ("gen", "general"):
        return "general"
    if stage in ("pri", "pre", "primary"):
        return "primary"
    raise InvalidInput(f"Unreviewed stage label {source_stage!r}.")


# --- registration ----------------------------------------------------------------------------------
def ensure_federal_reference(connection: sqlite3.Connection) -> None:
    curation = load_federal()[0]
    for code, (name, level, method) in OFFICES.items():
        connection.execute("INSERT OR IGNORE INTO office VALUES (?,?,?,?,?)", (office_id(code), code, name, level, method))
    for party in curation["parties"]:
        connection.execute("INSERT OR IGNORE INTO party VALUES (?,?,?)", (party_id(party["key"]), party["name"], party["key"]))
    for person in curation["persons"]:
        connection.execute("INSERT OR IGNORE INTO person VALUES (?,?,?,0)",
                           (person_id(person["key"]), person["name"], person["key"]))


def election_key(year: int, kind: str = "general") -> str:
    return f"us-general-{general_election_date(year).isoformat()}" if kind == "general" else f"us-{kind}-{year}"


class Registrar:
    """Idempotent, cached registration of elections, contests, units, candidacies and options."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.states = state_versions(connection)
        self.seen: set[tuple] = set()
        self.statements: list[tuple[str, tuple]] = []

    def _once(self, key: tuple, statement: str, values: tuple) -> None:
        if key not in self.seen:
            self.seen.add(key)
            self.connection.execute(statement, values)

    def state_unit(self, postal: str) -> str:
        return unit_id(f"state:{self.states[postal]['fips']}")

    def election(self, year: int, kind: str = "general") -> tuple[str, str]:
        key = election_key(year, kind)
        if kind == "general":
            day = general_election_date(year).isoformat()
            values = (election_id(key), f"{year} United States general election", day, day, "day", year,
                      geo_id("nation:US"), "general", key)
        else:
            cycle = year if year % 2 == 0 else year - 1
            values = (election_id(key), f"{year} {kind.replace('-', ' ')} elections (dates not given by source)",
                      f"{year}-01-01", f"{year}-12-31", "year", cycle, geo_id("nation:US"), kind, key)
        self._once(("election", key), "INSERT OR IGNORE INTO election_event VALUES (?,?,?,?,?,?,?,?,?)", values)
        return election_id(key), key

    def contest(self, *, office: str, year: int, postal: str, seat_key: str, stage: str = "general",
                contest_type: str = "regular", label: str | None = None) -> str:
        if postal not in self.states:
            raise InvalidInput(f"State geography for {postal} is missing; import Census state boundaries first.")
        regular_general = stage == "general" and contest_type == "regular" and year % 2 == 0
        kind = "general" if regular_general else f"{contest_type}-{stage}"
        event, key = self.election(year, kind)
        identifier = contest_id(key, office, seat_key, stage, contest_type)
        cycle = year if year % 2 == 0 else year - 1
        name = self.states[postal]["name"]
        text = label or f"{year} {OFFICES[office][0]} - {name} ({seat_key.split(':', 2)[-1]}, {contest_type} {stage})"
        self._once(("contest", identifier), "INSERT OR IGNORE INTO contest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            identifier, event, office_id(office), seat_key, self.states[postal]["geo_version_id"], stage, contest_type,
            "popular_vote_for_electors" if office == "president" else "popular_vote",
            voting_system(office, postal, cycle), 1, None, None, None, text))
        return identifier

    def president_contests(self, year: int) -> dict[str, str]:
        contests = {}
        for postal in states():
            for seat in president_seats(postal, year):
                part = seat.split(":")[-1]
                label = {"at-large": "At-large presidential electors"}.get(
                    part, f"Presidential elector, {part.upper()}" if part.startswith("cd") else "Presidential electors")
                contests[seat] = self.contest(office="president", year=year, postal=postal, seat_key=seat,
                                              label=f"{year} President - {self.states[postal]['name']} ({label})")
        return contests

    def district_unit(self, postal: str, district: int, year: int, *, delegate: bool = False) -> str:
        congress = congress_for_election(year if year % 2 == 0 else year - 1)
        code = "98" if delegate else f"{0 if house_seats(postal, year) == 1 else district:02d}"
        natural = f"cd:{postal}:{code}:congress-{congress}"
        label = (f"{self.states[postal]['name']} delegate district" if delegate else
                 f"{self.states[postal]['name']} at-large" if code == "00" else f"{self.states[postal]['name']} district {int(code)}")
        self._once(("unit", natural), "INSERT OR IGNORE INTO reporting_unit VALUES (?,?,?,?,?,?,?,?)", (
            unit_id(natural), "congressional_district", None, self.state_unit(postal), label,
            f"{congress}:{postal}:{code}", natural, f"District as constituted for the {congress}th Congress."))
        return unit_id(natural)

    def party(self, label: str) -> str | None:
        text = " ".join(label.upper().split())
        if text in ("", "NA", "N/A"):
            return None
        key = "source-label:" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        curated = {"DEMOCRAT": "democratic", "DEMOCRATIC": "democratic", "REPUBLICAN": "republican",
                   "LIBERTARIAN": "libertarian", "GREEN": "green"}.get(text)
        key = curated or key
        self._once(("party", key), "INSERT OR IGNORE INTO party VALUES (?,?,?)", (party_id(key), text.title(), key))
        return party_id(key)

    def ticket(self, contest: str, year: int, person_key: str) -> str:
        """Candidacy for a curated presidential ticket; returns its natural key."""
        curation = load_federal()[0]
        people = {person["key"]: person for person in curation["persons"]}
        ticket = next(item for item in curation["tickets"][str(year)] if item["president"] == person_key)
        natural = f"ticket:{person_key}"
        identifier = candidacy_id(contest, natural)
        if ("candidacy", identifier) not in self.seen:
            self.seen.add(("candidacy", identifier))
            party = party_id(ticket["party"])
            self.connection.execute("INSERT OR IGNORE INTO candidacy VALUES (?,?,?,?,?,?,?)", (
                identifier, contest, people[person_key]["name"], "ticket", party, "on_ballot_or_reported", natural))
            for key, role in ((person_key, "president"), (ticket["vice_president"], "vice_president")):
                self.connection.execute("INSERT OR IGNORE INTO candidacy_member VALUES (?,?,?)",
                                        (identifier, person_id(key), role))
        return natural

    def named(self, contest: str, raw_name: str) -> str:
        natural = f"name:{normalize_name(raw_name)}"
        self._once(("candidacy", candidacy_id(contest, natural)), "INSERT OR IGNORE INTO candidacy VALUES (?,?,?,?,?,?,?)", (
            candidacy_id(contest, natural), contest, " ".join(raw_name.replace('"', " ").split()).title(), "candidate",
            None, "source_label_unresolved_person", natural))
        return natural

    def aggregate(self, contest: str, key: str, label: str) -> str:
        self._once(("candidacy", candidacy_id(contest, key)), "INSERT OR IGNORE INTO candidacy VALUES (?,?,?,?,?,?,?)", (
            candidacy_id(contest, key), contest, label, "aggregate", None, "source_aggregate", key))
        return key

    def option(self, contest: str, candidacy_key: str, *, kind: str, label: str, party: str | None = None,
               line: str = "") -> str:
        natural = f"{candidacy_key}|{line}" if line else candidacy_key
        identifier = option_id(contest, natural)
        self._once(("option", identifier), "INSERT OR IGNORE INTO ballot_option VALUES (?,?,?,?,?,?,?)", (
            identifier, contest, candidacy_id(contest, candidacy_key), party, kind, label, natural))
        return identifier


AGGREGATE_LABELS = {"aggregate-write-in": ("Write-ins and scattering (source aggregate)", "aggregate_write_in"),
                    "aggregate-other": ("Other (source aggregate)", "aggregate_other"),
                    "aggregate-unnamed": ("Unnamed line (source aggregate)", "aggregate_other"),
                    "none-of-these-candidates": ("None of these candidates", "none_of_these"),
                    "aggregate-other-mit": ("Other (MIT aggregate)", "aggregate_other")}


def stable_set_id(identity: str, contest: str, partition: str) -> str:
    return stable_id("set", identity, contest, partition)
