"""Stopgap reader for the consolidated ``Marks`` sheet of the analyst workbook.

The Marks sheet is the analyst's manual consolidation of all ten broker
statements: one (trade-ref, MTM) column pair per broker, side by side
(plan section 1.4). Until raw per-broker statement files are available
(plan Q2), this reader stands in for the whole Phase-2 parser layer so the
pipeline can run end-to-end.

Values are emitted AS REPORTED (no sign flip); apply
``parsers.base.apply_sign_convention`` downstream.
"""

from __future__ import annotations

from pathlib import Path

import xlrd

from derivative_pricing.schema import Broker, BrokerMark, Lineage

# 0-based (id_col, mtm_col) pairs, verified from the test workbook header rows.
MARKS_LAYOUT: dict[Broker, tuple[int, int]] = {
    Broker.BARCLAYS: (0, 1),
    Broker.BNP_PARIBAS: (3, 4),
    Broker.MORGAN_STANLEY: (6, 7),
    Broker.GOLDMAN_SACHS: (9, 10),
    Broker.WELLS_FARGO: (12, 13),
    Broker.BAML: (15, 16),
    Broker.CREDIT_SUISSE: (19, 20),
    Broker.NOMURA: (23, 24),
    Broker.JP_MORGAN: (26, 27),
    Broker.HSBC: (29, 30),
}

_HEADER_ROWS = 2  # row 0 = broker banner, row 1 = column sub-headers


def _normalize_ref(value) -> str | None:
    """Trade refs may arrive as text or as numeric cells (e.g. BAML's
    10-digit IDs). Integral floats are rendered without the trailing .0."""
    if value is None:
        return None
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return str(value)
    text = str(value).strip()
    return text or None


def read_marks_sheet(path: Path | str, sheet_name: str = "Marks") -> list[BrokerMark]:
    path = Path(path)
    book = xlrd.open_workbook(str(path), on_demand=True)
    sheet = book.sheet_by_name(sheet_name)
    marks: list[BrokerMark] = []
    for broker, (id_col, mtm_col) in MARKS_LAYOUT.items():
        if mtm_col >= sheet.ncols:
            continue
        for row in range(_HEADER_ROWS, sheet.nrows):
            ref = _normalize_ref(sheet.cell_value(row, id_col))
            mtm_cell = sheet.cell(row, mtm_col)
            if ref is None or mtm_cell.ctype != xlrd.XL_CELL_NUMBER:
                continue
            marks.append(
                BrokerMark(
                    broker=broker,
                    trade_ref=ref,
                    mtm_local=float(mtm_cell.value),
                    lineage=Lineage(
                        source_file=path.name, sheet=sheet_name, row=row + 1
                    ),
                )
            )
    return marks