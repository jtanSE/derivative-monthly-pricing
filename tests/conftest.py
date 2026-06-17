from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_WORKBOOK = REPO_ROOT / "ALIC MTM Prices for Call Options Test - 31 March 2026.xls"


@pytest.fixture(scope="session")
def workbook_path() -> Path:
    if not TEST_WORKBOOK.exists():
        pytest.skip(f"sanitized test workbook not present: {TEST_WORKBOOK.name}")
    return TEST_WORKBOOK