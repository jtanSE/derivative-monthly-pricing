"""Dating a run from the statements rather than a typed-in default.

The bug these guard: the UI's month-end date used to default to a hard-coded
2026-03-31. Dropping in August statements produced a sheet stamped March whose
maturity screen compared every expiry against March and therefore flagged
nothing as matured, even though four Nomura trades had expired. Nothing in the
output said the date was wrong.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from derivative_pricing.adaptive.agent import SPECS_DIR, load_spec
from derivative_pricing.adaptive.spec import ColumnRule, ParserSpec, ValuationDateRule
from derivative_pricing.adaptive.statement_date import parse_date_text, statement_date
from derivative_pricing.run_month import detect_as_of
from derivative_pricing.schema import Broker

# Real month-end statement folders (git-ignored; these tests skip without them).
_DATA = Path(__file__).resolve().parents[1] / "data"

# --- date text parsing ------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("2026-08-31", "2026-08-31"),      # Nomura
    ("31-Aug-2026", "2026-08-31"),     # Morgan Stanley
    ("28-Aug-2026", "2026-08-28"),     # HSBC
    ("31-Aug-26", "2026-08-31"),       # BAML (2-digit year)
    ("Aug 31, 2026", "2026-08-31"),    # Goldman
    ("31 August 2026", "2026-08-31"),  # Barclays prose
    ("31 Aug 2026", "2026-08-31"),     # BNP prose
    ("  31 Aug 2026.  ", "2026-08-31"),  # trailing punctuation / whitespace
])
def test_parse_date_text_accepts_every_broker_spelling(text, expected):
    assert parse_date_text(text) == expected


@pytest.mark.parametrize("text", ["", None, "n/a", "Valuation Date", "31/13/2026"])
def test_parse_date_text_rejects_non_dates(text):
    # A wrong-but-plausible date is worse than none: it would silently mis-date
    # a run, which is the exact failure this module exists to prevent.
    assert parse_date_text(text) is None


def test_two_digit_year_is_not_truncated_by_the_four_digit_format():
    # "%d-%b-%y" must be tried after "%d-%b-%Y" or 31-Aug-2026 parses as year 20.
    assert parse_date_text("31-Aug-2026") == "2026-08-31"


# --- the three extraction modes ---------------------------------------------


def _spec(**kw) -> ParserSpec:
    return ParserSpec(
        broker=Broker.NOMURA,
        header_anchor="Ref",
        columns=[
            ColumnRule(field="trade_ref", header_contains="Ref", kind="text"),
            ColumnRule(field="mtm_local", header_contains="MTM", kind="number"),
        ],
        **kw,
    )


def _write(tmp_path: Path, rows: list[list], name: str = "s.xlsx") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    path = tmp_path / name
    wb.save(path)
    return path


def test_column_mode_reads_a_per_row_valuation_date(tmp_path):
    path = _write(tmp_path, [
        ["Ref", "MTM", "valuationDate"],
        ["A1", 100.0, "2026-08-31"],
        ["A2", 200.0, "2026-08-31"],
    ])
    spec = _spec(valuation_date=ValuationDateRule(mode="column",
                                                  header_contains="valuationDate"))
    assert statement_date(spec, path) == "2026-08-31"


def test_label_mode_reads_past_the_padding_cells(tmp_path):
    # BAML puts "Value Date" in col B and the date in col D, with C empty.
    path = _write(tmp_path, [
        ["", "Value Date", "", "31-Aug-26"],
        [],
        ["Ref", "MTM"],
        ["A1", 100.0],
    ])
    spec = _spec(valuation_date=ValuationDateRule(mode="label",
                                                  label_contains="Value Date"))
    assert statement_date(spec, path) == "2026-08-31"


def test_regex_mode_takes_the_capture_group_not_the_first_date(tmp_path):
    # BNP quotes TWO dates and only the close-of-business one matches the marks.
    prose = ("We are pleased to provide you with the indicative valuation as of "
             "01 Sep 2026, based on the Close of business 31 Aug 2026.")
    path = _write(tmp_path, [[prose], [], ["Ref", "MTM"], ["A1", 100.0]])
    spec = _spec(valuation_date=ValuationDateRule(
        mode="regex", pattern=r"close of business\s+(\d{1,2}\s+[A-Za-z]+\s+\d{2,4})"))
    assert statement_date(spec, path) == "2026-08-31"


def test_no_rule_means_no_date_rather_than_today(tmp_path):
    path = _write(tmp_path, [["Ref", "MTM"], ["A1", 100.0]])
    assert statement_date(_spec(), path) is None


def test_missing_column_returns_none_instead_of_raising(tmp_path):
    path = _write(tmp_path, [["Ref", "MTM"], ["A1", 100.0]])
    spec = _spec(valuation_date=ValuationDateRule(mode="column",
                                                  header_contains="nope"))
    assert statement_date(spec, path) is None


def test_unreadable_file_returns_none_instead_of_raising(tmp_path):
    # Dating a run must never be what breaks parsing it.
    bad = tmp_path / "not-a-workbook.xlsx"
    bad.write_bytes(b"certainly not a zip archive")
    spec = _spec(valuation_date=ValuationDateRule(mode="column",
                                                  header_contains="valuationDate"))
    assert statement_date(spec, bad) is None


# --- every shipped spec, against the real statements ------------------------

_EXPECTED = {
    "2026-03": "2026-03-31",
    "2026-02": "2026-02-27",  # February month-end fell on a Friday
}


@pytest.mark.parametrize("folder", sorted(_EXPECTED))
def test_every_readable_statement_dates_itself_to_the_folder_month(folder):
    d = _DATA / folder
    if not d.exists():
        pytest.skip(f"broker statements not present: {folder}")
    detected, stated = detect_as_of(d)
    if not stated:
        pytest.skip(f"no readable statements in {folder}")
    assert detected == _EXPECTED[folder]
    # Each individual broker agrees, not just the consensus.
    assert set(stated.values()) == {_EXPECTED[folder]}, stated


def test_all_shipped_specs_declare_where_their_date_lives():
    # A spec without this rule silently falls back to a typed-in date, which is
    # the failure mode this whole module exists to remove. New specs should
    # carry the rule; if a broker genuinely omits the date, remove it from here
    # deliberately rather than by forgetting.
    missing = [p.stem for p in sorted(SPECS_DIR.glob("*.json"))
               if load_spec(p).valuation_date is None]
    assert missing == [], f"specs with no valuation_date rule: {missing}"


# --- consensus --------------------------------------------------------------


def test_consensus_tolerates_one_broker_closing_early(monkeypatch, tmp_path):
    # HSBC's Aug-2026 statement closes 28 Aug while the rest close 31 Aug. One
    # early file must not drag the whole run onto the wrong date.
    from derivative_pricing import run_month as rm

    dates = iter(["2026-08-31", "2026-08-31", "2026-08-28"])
    specs = iter([Broker.BAML, Broker.NOMURA, Broker.HSBC])

    for i in range(3):
        (tmp_path / f"f{i}.xlsx").write_bytes(b"x")

    monkeypatch.setattr(rm, "statement_date", lambda spec, path: next(dates))
    fake = [type("S", (), {"broker": next(specs)})() for _ in range(3)]
    seq = iter(fake)
    monkeypatch.setattr("derivative_pricing.adaptive.router.identify",
                        lambda path, specs=None: [next(seq)])

    detected, stated = rm.detect_as_of(tmp_path)
    assert detected == "2026-08-31"
    assert stated["HSBC"] == "2026-08-28"
