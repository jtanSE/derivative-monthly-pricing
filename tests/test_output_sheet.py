from derivative_pricing.adaptive.agent import SPECS_DIR, load_spec
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.output import (
    build_rows_from_marks,
    option_type,
    split_marks_by_option_type,
    write_call_options_sectioned_xlsx,
)
from derivative_pricing.output.call_options_sheet import _signed_shares
from derivative_pricing.schema import Broker, BrokerMark, Lineage
from derivative_pricing.workbook import read_call_options


def _mark(ref, qty, direction, price, underlying="S&P 500", mtm=1000.0):
    return BrokerMark(
        broker=Broker.BNP_PARIBAS,
        trade_ref=ref,
        mtm_local=mtm,
        currency="USD",
        extra={"quantity": qty, "direction": direction, "underlying_price": price,
               "underlying": underlying},
        lineage=Lineage(source_file="x.xls", sheet="s", row=1),
    )


def _typed_mark(ref, put_call, mtm=1000.0):
    return BrokerMark(
        broker=Broker.BNP_PARIBAS, trade_ref=ref, mtm_local=mtm, currency="USD",
        extra={"put_call": put_call, "quantity": 10.0, "strike": 6500.0,
               "underlying_price": 6500.0, "underlying": "S&P 500"},
        lineage=Lineage(source_file="x.xls", sheet="s", row=1),
    )


def test_option_type_classification():
    assert option_type(_typed_mark("A", "CALL")) == "call"
    assert option_type(_typed_mark("B", "C")) == "call"
    assert option_type(_typed_mark("C", "PUT")) == "put"
    assert option_type(_typed_mark("D", "P")) == "put"
    assert option_type(_typed_mark("E", "put")) == "put"
    # blank Call/Put (the MCB net-MTM structures) is treated as a call,
    # matching ALIC, which books those call-spreads in the Call Options sheet.
    assert option_type(_typed_mark("F", None)) == "call"


def test_split_preserves_every_mark():
    marks = [_typed_mark("c1", "CALL"), _typed_mark("p1", "PUT"),
             _typed_mark("mcb", None), _typed_mark("p2", "P")]
    calls, puts = split_marks_by_option_type(marks)
    assert len(calls) == 2 and len(puts) == 2      # c1 + mcb ; p1 + p2
    assert len(calls) + len(puts) == len(marks)     # nothing dropped
    assert {m.trade_ref for m in puts} == {"p1", "p2"}


def test_put_sheet_carries_put_label(tmp_path):
    import openpyxl

    puts = [_typed_mark("P1", "PUT", mtm=500.0)]
    rows = build_rows_from_marks(puts, fx_rates={"USD": 1.0})
    out = write_call_options_sectioned_xlsx(
        rows, tmp_path / "puts.xlsx", as_of="2026-03-31", sheet_label="Put Options")
    ws = openpyxl.load_workbook(out).active
    assert ws.title == "Put Options"
    col_a = " ".join(str(ws.cell(row=r, column=1).value or "")
                     for r in range(1, ws.max_row + 1))
    assert "ALIC Put Options" in col_a


def test_signed_shares_rule():
    assert _signed_shares(100.0, "Long") == 100.0
    assert _signed_shares(100.0, "Short") == -100.0
    assert _signed_shares(100.0, "short") == -100.0  # case-insensitive
    assert _signed_shares(100.0, None) == 100.0  # default long-ish
    assert _signed_shares(None, "Short") is None


def test_short_position_yields_negative_notional():
    rows = build_rows_from_marks(
        [_mark("S1", 10.0, "Short", 6500.0)], fx_rates={"USD": 1.0}
    )
    r = rows[0]
    assert r.shares == -10.0
    assert r.notional_local == -65000.0  # negative for short
    assert r.price_local is not None and r.price_local < 0  # mtm(+) / shares(-)


def test_long_position_positive():
    rows = build_rows_from_marks(
        [_mark("L1", 10.0, "Long", 6500.0)], fx_rates={"USD": 1.0}
    )
    assert rows[0].notional_local == 65000.0


def test_sectioned_output_has_banners_subtotals_grandtotal(tmp_path):
    import openpyxl

    marks = [
        _mark("B1", 10.0, "Long", 100.0, mtm=500.0),
        _mark("B2", 5.0, "Short", 100.0, mtm=-200.0),  # short -> negative notional
    ]
    marks[1].broker = Broker.HSBC  # force a second section
    rows = build_rows_from_marks(marks, fx_rates={"USD": 1.0})
    out = write_call_options_sectioned_xlsx(rows, tmp_path / "alic.xlsx", as_of="2026-03-31")

    ws = openpyxl.load_workbook(out).active
    col_a = [ws.cell(row=r, column=1).value for r in range(1, ws.max_row + 1)]
    text = " ".join(str(x) for x in col_a if x)
    assert "BNP Paribas" in text and "HSBC" in text          # banners
    assert "subtotal" in text.lower()                         # F7 subtotal rows
    assert "GRAND TOTAL" in text                              # F8 grand total

    # F7: notional subtotal is positive-only (short leg excluded)
    sub_row = next(r for r in range(1, ws.max_row + 1)
                   if str(ws.cell(row=r, column=1).value or "").endswith("subtotal"))
    assert ws.cell(row=sub_row, column=7).value == 1000.0  # only the long leg's notional


def test_matches_alic_after_sign_fix(bnp_statement_path, workbook_path):
    """With the Long/Short sign applied, the system's Notional and Price
    reproduce the analyst's ALIC sheet for all but the manually-overridden
    trades (which also drive the MTM differences)."""
    marks = run_spec(load_spec(SPECS_DIR / "bnp.json"), bnp_statement_path)
    rows = {r.trade_reference: r
            for r in build_rows_from_marks(marks, {"USD": 1.0})}
    wb = read_call_options(workbook_path)
    alic = {r.trade_ref: r for r in wb.rows if r.broker == Broker.BNP_PARIBAS}
    common = set(rows) & set(alic)

    def close(a, b):
        if a is None or b is None:
            return False
        return abs(a - b) <= 0.02 * max(abs(a), abs(b), 1)

    notl_ok = sum(1 for ref in common if close(rows[ref].notional_local, alic[ref].notional_local))
    assert notl_ok / len(common) >= 0.90, f"notional match only {notl_ok}/{len(common)}"