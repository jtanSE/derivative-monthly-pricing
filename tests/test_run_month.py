import shutil
from pathlib import Path

from derivative_pricing.run_month import run_month


def test_run_month_processes_a_folder(tmp_path, bnp_statement_path, baml_statement_path,
                                      gs_statement_path, hsbc_statement_path):
    indir = tmp_path / "statements"
    indir.mkdir()
    for p in (bnp_statement_path, baml_statement_path, gs_statement_path, hsbc_statement_path):
        shutil.copy(p, indir / p.name)

    summary = run_month(indir, tmp_path / "out", as_of="2026-03-31")

    brokers = {broker for _, broker, _ in summary.known}
    assert brokers == {"BNP Paribas", "BAML", "Goldman Sachs", "HSBC"}
    assert summary.total_marks > 600
    assert not summary.unknown and not summary.ambiguous
    assert Path(summary.output_xlsx).exists()
    assert Path(summary.output_csv).exists()


def test_run_month_flags_unknown(tmp_path, workbook_path):
    indir = tmp_path / "statements"
    indir.mkdir()
    shutil.copy(workbook_path, indir / workbook_path.name)  # not a broker statement

    summary = run_month(indir, tmp_path / "out", as_of="2026-03-31")
    assert workbook_path.name in summary.unknown
    assert not summary.known


def test_run_month_never_double_counts_a_broker(tmp_path, baml_statement_path):
    """Two files for the same broker in one folder must NOT both be summed —
    the second is flagged as a duplicate and excluded (ops control: silent
    double counting would inflate every total)."""
    indir = tmp_path / "statements"
    indir.mkdir()
    shutil.copy(baml_statement_path, indir / "baml_a.xls")
    shutil.copy(baml_statement_path, indir / "baml_b.xls")

    summary = run_month(indir, tmp_path / "out", as_of="2026-03-31")
    brokers = [b for _, b, _ in summary.known]
    assert brokers.count("BAML") == 1
    assert len(summary.duplicates) == 1
    assert summary.duplicates[0][1] == "BAML"
    assert summary.total_marks == 70  # one file's marks, not 140


def test_load_market_data_accepts_fx_only(tmp_path):
    fx = tmp_path / "fx.csv"
    fx.write_text("currency,rate_to_usd\nUSD,1.0\nEUR,1.1553\n", encoding="utf-8")
    from derivative_pricing.reference.market_data import load_market_data

    md = load_market_data("2026-03-31", fx_csv=fx, index_csv=None)
    assert md.fx("EUR") == 1.1553
    assert md.index_prices == {}
