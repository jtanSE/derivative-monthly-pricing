from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_WORKBOOK = REPO_ROOT / "ALIC MTM Prices for Call Options Test - 31 March 2026.xls"
BNP_STATEMENT = REPO_ROOT / "BNP AUGUSTAR LIFE INSURANCE COMPANY_01 Apr 2026 (2).xls"
BAML_STATEMENT = REPO_ROOT / "BAML 03-31-2026_Results_82654.xls"


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