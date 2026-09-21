"""Guards for the ways a run can be incomplete without looking incomplete.

Each of these reproduces something an August 2026 run did silently:

  * Goldman and Nomura produced whole sections with no notional (no index price
    to multiply by), and their subtotals read "0.00" -- which rolls into a grand
    total that looks like a control total while excluding 801 of 1,609 rows.
  * Nomura writes "Compound" into its ``underlying`` column on the rows whose
    productType is also "Compound", giving an Index column that looks populated
    but can never match an index price.
  * A password-protected JPM file was reported as "Unknown format - need
    onboarding", sending a human to draft a spec for a file that cannot be
    opened at all.
"""

from __future__ import annotations

import openpyxl
import pytest

from derivative_pricing.adaptive.agent import onboard_file
from derivative_pricing.adaptive.router import route_folder
from derivative_pricing.output.call_options_sheet import (
    CallOptionsOutputRow,
    write_call_options_sectioned_xlsx,
)
from derivative_pricing.reference.indices import canonicalize_index, is_index_name

# --- index names that are not index names -----------------------------------


@pytest.mark.parametrize("raw", ["Compound", "compound", "ExoticOption", "Option",
                                 "NOTAPPLICABLE", "not applicable", "n/a", "None"])
def test_product_types_are_not_accepted_as_index_names(raw):
    assert is_index_name(raw) is False
    # Rejected to empty rather than passed through: a populated-looking Index
    # that can never price is worse than a visibly blank one.
    assert canonicalize_index(raw) == ""


@pytest.mark.parametrize(("raw", "expected"), [
    ("S&P 500", "S&P 500"),
    ("s&p 500", "S&P 500"),          # canonical spelling restored
    ("Russell 2000", "Russell 2000"),
    ("  Russell   2000 ", "Russell 2000"),
    (".SPX", ".SPX"),                # unknown tickers pass through untouched
])
def test_real_index_names_still_canonicalize(raw, expected):
    assert is_index_name(raw) is True
    assert canonicalize_index(raw) == expected


# --- notional absence vs. zero ----------------------------------------------


def _row(broker: str, *, notional: float | None, mtm: float) -> CallOptionsOutputRow:
    return CallOptionsOutputRow(
        cusip=None, index="S&P 500", trade_reference=f"{broker}-1", strike_rate=100.0,
        maturity_date="2027-01-15", currency="USD",
        notional_local=notional, notional_usd=notional,
        mtm_local=mtm, mtm_usd=mtm, shares=10.0, price_local=None, broker=broker,
    )


def _cells(path):
    ws = openpyxl.load_workbook(path, data_only=True).active
    return {ws.cell(r, 1).value: [ws.cell(r, c).value for c in range(1, 14)]
            for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value}


def test_a_section_with_no_notional_reports_blank_not_zero(tmp_path):
    rows = [
        _row("BAML", notional=1000.0, mtm=50.0),
        _row("Nomura", notional=None, mtm=70.0),
    ]
    out = write_call_options_sectioned_xlsx(rows, tmp_path / "o.xlsx", as_of="2026-08-31")
    cells = _cells(out)
    # Notional (USD) is column 8.
    assert cells["BAML subtotal"][7] == 1000.0
    assert cells["Nomura subtotal"][7] is None, "no-notional section must not read 0.00"
    # MTM (column 10) is complete for both and still totals.
    assert cells["Nomura subtotal"][9] == 70.0
    assert cells["GRAND TOTAL"][7] == 1000.0
    assert cells["GRAND TOTAL"][9] == 120.0


def test_partial_notional_coverage_is_called_out_in_the_sheet(tmp_path):
    rows = [
        _row("BAML", notional=1000.0, mtm=50.0),
        _row("Nomura", notional=None, mtm=70.0),
        _row("Goldman Sachs", notional=None, mtm=30.0),
    ]
    out = write_call_options_sectioned_xlsx(rows, tmp_path / "o.xlsx", as_of="2026-08-31")
    notes = [v for v in _cells(out) if isinstance(v, str) and v.startswith("NOTE:")]
    assert len(notes) == 1, "an incomplete notional total must say so"
    assert "Goldman Sachs" in notes[0] and "Nomura" in notes[0]
    assert "2 of 3 sections" in notes[0]
    assert "MTM is complete" in notes[0]


def test_no_note_when_every_section_has_notional(tmp_path):
    rows = [_row("BAML", notional=1000.0, mtm=50.0),
            _row("HSBC", notional=2000.0, mtm=70.0)]
    out = write_call_options_sectioned_xlsx(rows, tmp_path / "o.xlsx", as_of="2026-08-31")
    assert not [v for v in _cells(out) if isinstance(v, str) and v.startswith("NOTE:")]


def test_no_note_when_nothing_has_notional(tmp_path):
    # Every section missing means market data was simply never supplied -- that
    # is a different message, not a per-broker exclusion note.
    rows = [_row("BAML", notional=None, mtm=50.0), _row("HSBC", notional=None, mtm=70.0)]
    out = write_call_options_sectioned_xlsx(rows, tmp_path / "o.xlsx", as_of="2026-08-31")
    assert not [v for v in _cells(out) if isinstance(v, str) and v.startswith("NOTE:")]


# --- unreadable is not the same as unrecognized ------------------------------


def _unreadable_xls(path):
    # Real BIFF header, then garbage: opens as an .xls and fails while parsing,
    # the same shape of failure as a password-protected workbook.
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    return path


def test_route_folder_separates_unreadable_from_unrecognized(tmp_path):
    _unreadable_xls(tmp_path / "locked.xls")
    wb = openpyxl.Workbook()
    wb.active.append(["Nothing", "Recognizable", "Here"])
    wb.save(tmp_path / "stranger.xlsx")

    by_name = {r.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: r
               for r in route_folder(tmp_path)}

    locked = by_name["locked.xls"]
    assert locked.status == "unknown"
    assert locked.error, "an unreadable file must carry why it could not be read"

    stranger = by_name["stranger.xlsx"]
    assert stranger.status == "unknown"
    assert stranger.error is None, "a readable-but-unrecognized file has no read error"


def test_run_month_reports_unreadable_files_apart_from_unknown_formats(tmp_path):
    from derivative_pricing.run_month import run_month

    _unreadable_xls(tmp_path / "locked.xls")
    wb = openpyxl.Workbook()
    wb.active.append(["Nothing", "Recognizable", "Here"])
    wb.save(tmp_path / "stranger.xlsx")

    s = run_month(tmp_path, tmp_path / "out", as_of="2026-08-31")
    assert [n for n, _ in s.unreadable] == ["locked.xls"]
    assert s.unknown == ["stranger.xlsx"]


def test_onboarding_an_unreadable_file_does_not_raise_and_spends_nothing(tmp_path):
    res = onboard_file(_unreadable_xls(tmp_path / "locked.xls"))
    assert res.status == "unreadable"
    # No model was invoked, so no tokens can have been billed for it.
    assert res.usage is None
    assert res.spec is None
