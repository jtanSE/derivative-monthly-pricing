"""Format-agnostic workbook reader.

Broker statements arrive as both legacy .xls (xlrd, with the SUPBOOK-tolerance
patch) and modern .xlsx (openpyxl). The spec engine should not care which.
``load_grid`` returns a uniform Grid of typed cells (text / number / date /
empty) so the same parsing logic runs against either format.

Discovered when HSBC turned up as .xlsx while BNP/BAML/GS were .xls.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path


class Grid:
    """Uniform 2-D view of one worksheet.

    Each cell is normalized to one of: None, str, float, or datetime.date.
    """

    def __init__(self, name: str, cells: list[list]):
        self.name = name
        self._cells = cells
        self.nrows = len(cells)
        self.ncols = max((len(r) for r in cells), default=0)

    def _raw(self, r: int, c: int):
        if 0 <= r < self.nrows and 0 <= c < len(self._cells[r]):
            return self._cells[r][c]
        return None

    def text(self, r: int, c: int) -> str:
        v = self._raw(r, c)
        if v is None:
            return ""
        if isinstance(v, float):
            return str(int(v)) if v == int(v) else str(v)
        if isinstance(v, dt.date):
            return v.isoformat()
        return str(v).strip()

    def num(self, r: int, c: int):
        v = self._raw(r, c)
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        # Some brokers (e.g. Morgan Stanley) export every number as TEXT with
        # thousands separators -- "1,460,405.0273". Coerce those; genuinely
        # non-numeric text (dates, labels, currencies) still returns None.
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if s:
                try:
                    return float(s)
                except ValueError:
                    return None
        return None

    def date_iso(self, r: int, c: int):
        v = self._raw(r, c)
        if isinstance(v, dt.date):
            return v.isoformat()
        return self.text(r, c) or None


def _load_xls(path: Path) -> Grid:
    import xlrd

    from derivative_pricing.parsers.xls_compat import open_workbook

    book = open_workbook(path, formatting_info=False)
    sheet = book.sheet_by_index(0)
    cells: list[list] = []
    for r in range(sheet.nrows):
        row = []
        for c in range(sheet.ncols):
            cell = sheet.cell(r, c)
            t = cell.ctype
            if t in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                row.append(None)
            elif t == xlrd.XL_CELL_DATE:
                try:
                    tup = xlrd.xldate_as_tuple(cell.value, book.datemode)
                    row.append(dt.datetime(*tup).date() if tup[:3] != (0, 0, 0) else cell.value)
                except Exception:
                    row.append(cell.value)
            elif t == xlrd.XL_CELL_NUMBER:
                row.append(float(cell.value))
            elif t == xlrd.XL_CELL_BOOLEAN:
                row.append(str(bool(cell.value)))
            else:
                row.append(str(cell.value).strip())
        cells.append(row)
    return Grid(sheet.name, cells)


def _load_xlsx(path: Path) -> Grid:
    import openpyxl

    # read_only=True trusts the workbook's cached <dimension> tag; some broker
    # exports (e.g. Nomura) ship a bogus "A1:A1" dimension that hides the whole
    # table. Loading non-read-only recomputes dimensions from the actual cells.
    # Broker statements are small (hundreds of rows), so the cost is negligible.
    wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    sheet = wb[wb.sheetnames[0]]
    cells: list[list] = []
    for row in sheet.iter_rows(values_only=True):
        out = []
        for v in row:
            if isinstance(v, dt.datetime):
                out.append(v.date())
            else:
                out.append(v)
        cells.append(list(out))
    wb.close()
    return Grid(sheet.title, cells)


def load_grid(path: Path | str) -> Grid:
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        return _load_xlsx(path)
    return _load_xls(path)
