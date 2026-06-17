"""Robust .xls opening for broker statements.

Some broker-exported .xls files carry truncated or non-standard SUPBOOK
records (external workbook links). xlrd 1.2 crashes parsing them with an
IndexError, even though the cell data is perfectly readable. We never need
external-link metadata to read marks, so we patch the SUPBOOK handler to skip
any record it cannot parse.

The patch is applied once, on import, and is a no-op for well-formed files.
Observed in the BNP "Option - Reg. MM" statement (April 2026).
"""

from __future__ import annotations

from pathlib import Path

import xlrd
import xlrd.book

_orig_handle_supbook = xlrd.book.Book.handle_supbook


def _safe_handle_supbook(self, data):
    try:
        return _orig_handle_supbook(self, data)
    except Exception:
        # Keep the supbook bookkeeping lists balanced so downstream parsing
        # of formulas/sheets doesn't index past the end.
        for attr in ("_supbook_types", "_supbook_locals_inx", "_supbook_addins_inx"):
            lst = getattr(self, attr, None)
            if lst is not None:
                lst.append(None)
        return


xlrd.book.Book.handle_supbook = _safe_handle_supbook


def open_workbook(path: Path | str, **kwargs):
    """xlrd.open_workbook with the SUPBOOK-tolerance patch active."""
    return xlrd.open_workbook(str(path), **kwargs)