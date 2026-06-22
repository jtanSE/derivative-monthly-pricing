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
