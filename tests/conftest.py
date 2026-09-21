from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# Month-end statements + confirmed workbooks live under data/<as-of>/ (git-ignored).
_MAR = REPO_ROOT / "data" / "2026-03"
TEST_WORKBOOK = _MAR / "ALIC MTM Prices for Call Options Test - 31 March 2026.xls"
BNP_STATEMENT = _MAR / "BNP AUGUSTAR LIFE INSURANCE COMPANY_01 Apr 2026 (2).xls"
BAML_STATEMENT = _MAR / "BAML 03-31-2026_Results_82654.xls"
GS_STATEMENT = _MAR / "GS_Ohio National Life Ins Co (EQ)_20260331.xls"
HSBC_STATEMENT = _MAR / "HSBC GV_AUGUSTAR LIFE INSURANCE COMPANY REG15_31Mar2026_070407.xlsx"
NOMURA_STATEMENT = _MAR / "Nomura_01_04_2026_05_45_11_AUGUSTAR_LIFE_IN_NGFP_CSA_41850065.XLSX"
# Barclays only has a February statement (multi-table CVGClientStatement).
_FEB = REPO_ROOT / "data" / "2026-02"
BARCLAYS_STATEMENT = _FEB / "Barclays CVGClientStatement Report for The Ohio National Life Insurance Company as at 27 Feb 2026.xls"


@pytest.fixture(scope="session")
def workbook_path() -> Path:
    if not TEST_WORKBOOK.exists():
        pytest.skip(f"sanitized test workbook not present: {TEST_WORKBOOK.name}")
    return TEST_WORKBOOK


@pytest.fixture(scope="session")
def bnp_statement_path() -> Path:
    # Git-ignored real-ish broker file; present locally, skipped in clean CI.
    if not BNP_STATEMENT.exists():
        pytest.skip(f"BNP statement not present: {BNP_STATEMENT.name}")
    return BNP_STATEMENT


@pytest.fixture(scope="session")
def baml_statement_path() -> Path:
    if not BAML_STATEMENT.exists():
        pytest.skip(f"BAML statement not present: {BAML_STATEMENT.name}")
    return BAML_STATEMENT


@pytest.fixture(scope="session")
def gs_statement_path() -> Path:
    if not GS_STATEMENT.exists():
        pytest.skip(f"GS statement not present: {GS_STATEMENT.name}")
    return GS_STATEMENT


@pytest.fixture(scope="session")
def hsbc_statement_path() -> Path:
    if not HSBC_STATEMENT.exists():
        pytest.skip(f"HSBC statement not present: {HSBC_STATEMENT.name}")
    return HSBC_STATEMENT


@pytest.fixture(scope="session")
def nomura_statement_path() -> Path:
    if not NOMURA_STATEMENT.exists():
        pytest.skip(f"Nomura statement not present: {NOMURA_STATEMENT.name}")
    return NOMURA_STATEMENT


@pytest.fixture(scope="session")
def barclays_statement_path() -> Path:
    if not BARCLAYS_STATEMENT.exists():
        pytest.skip(f"Barclays statement not present: {BARCLAYS_STATEMENT.name}")
    return BARCLAYS_STATEMENT