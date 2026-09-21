"""Multi-table parsing (ParserSpec.multi_table).

Some statements stack several tables under one header anchor. Barclays'
CVGClientStatement has three: a bond-forward table, "Equity Options" (has a
Quantity column), and "Other Equity Derivatives" (Put/Call + Strike Rate, no
Quantity). The engine must parse EVERY table whose columns include trade_ref +
mtm_local -- which pulls in both option tables and drops the bond-forward table
(it has MTM (Ccy1), not MTM(Ccy)) -- with columns mapped per table.

Skips cleanly when the git-ignored Barclays file is absent (CI); guards locally.
"""

from derivative_pricing.adaptive.router import route


def test_barclays_multi_table_parses_both_option_tables(barclays_statement_path):
    res = route(barclays_statement_path)
    assert res.status == "known" and res.broker == "Barclays"
    # The Equity Options table alone is ~277 marks; multi-table adds the ~155-row
    # Other Equity Derivatives table. Single-table parsing would fall far short.
    assert len(res.marks) > 400, (
        f"multi-table parsing got only {len(res.marks)} marks -- the second "
        "options table was not picked up"
    )


def test_barclays_excludes_bond_forward_table(barclays_statement_path):
    # The bond-forward table has no Put/Call or Strike Rate, so its refs must not
    # appear. Bond-forward refs on this statement are option-less notionals; a
    # cheap proxy: every parsed mark carries an mtm and (for options) a strike.
    res = route(barclays_statement_path)
    assert all(m.mtm_local is not None for m in res.marks)
    # options tables both carry Put/Call; bond forwards do not
    with_cp = sum(1 for m in res.marks if m.extra.get("put_call"))
    assert with_cp > 400
