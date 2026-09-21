"""Read the date a statement values its positions AS OF.

Why this exists: the as-of date used to be typed in by hand, defaulting to a
hard-coded month-end. Feed the app August statements with a March default still
selected and everything still "works" -- the sheet is stamped March, and the
maturity screen (which compares each expiry against the as-of) silently finds
nothing matured, because the whole book expires after March. The date is the one
input that must not be guessable, and every statement already states it.

Each broker states it somewhere different -- a per-row column, a labelled
preamble cell, or a sentence -- so ParserSpec.valuation_date declares which of
the three modes applies (see spec.ValuationDateRule).

Extraction never raises: a statement whose date can't be read returns None so
callers fall back to the user-supplied as-of rather than failing a whole run.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from derivative_pricing.adaptive.engine import _find_header_row, _norm
from derivative_pricing.adaptive.reader import Grid, load_grid
from derivative_pricing.adaptive.spec import ParserSpec

# Accepted spellings, in the order brokers actually use them. "%d-%b-%y" must
# come after "%d-%b-%Y" so "31-Aug-2026" is not truncated by the 2-digit parse.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %B %Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
)


def parse_date_text(text: str) -> str | None:
    """Parse a broker-formatted date into an ISO string, or None.

    Deliberately format-driven rather than a fuzzy parser: a wrong-but-plausible
    date (month/day swapped) is worse here than no date at all, since the whole
    point is to stop a mis-dated run.
    """
    s = " ".join(str(text or "").split()).strip().rstrip(".,;")
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _from_column(grid: Grid, spec: ParserSpec, header_contains: str) -> str | None:
    """First parseable value in the named column below the header row."""
    header_row = _find_header_row(grid, spec.header_anchor, spec.header_requires)
    want = _norm(header_contains)
    col = next((c for c in range(grid.ncols) if want in _norm(grid.text(header_row, c))), None)
    if col is None:
        return None
    for r in range(header_row + 1, grid.nrows):
        got = parse_date_text(grid.date_iso(r, col) or "")
        if got:
            return got
    return None


def _from_label(grid: Grid, label_contains: str, value_offset: int,
                search_rows: int) -> str | None:
    """Value sitting ``value_offset`` cells right of a labelled preamble cell.

    Brokers pad the preamble with merged/empty cells, so the offset is a
    starting point: scan rightwards from it for the first parseable date.
    """
    want = _norm(label_contains)
    for r in range(min(grid.nrows, search_rows)):
        for c in range(grid.ncols):
            if want not in _norm(grid.text(r, c)):
                continue
            for cc in range(c + value_offset, min(grid.ncols, c + value_offset + 6)):
                got = parse_date_text(grid.date_iso(r, cc) or "")
                if got:
                    return got
    return None


def _from_regex(grid: Grid, pattern: str, search_rows: int) -> str | None:
    """Date from the first capture group of ``pattern`` anywhere in the preamble."""
    rx = re.compile(pattern, re.I)
    for r in range(min(grid.nrows, search_rows)):
        for c in range(grid.ncols):
            text = grid.text(r, c)
            if not text:
                continue
            m = rx.search(text)
            if m and m.groups():
                got = parse_date_text(m.group(1))
                if got:
                    return got
    return None


def statement_date(spec: ParserSpec, path: Path | str,
                   grid: Grid | None = None) -> str | None:
    """ISO date this statement values as of, or None if it doesn't say.

    ``grid`` may be passed to avoid re-reading a workbook the caller already
    loaded. Any extraction failure -- missing rule, missing column, unreadable
    file, unparseable text -- returns None.
    """
    rule = spec.valuation_date
    if rule is None:
        return None
    try:
        g = grid if grid is not None else load_grid(path)
        if rule.mode == "column" and rule.header_contains:
            return _from_column(g, spec, rule.header_contains)
        if rule.mode == "label" and rule.label_contains:
            return _from_label(g, rule.label_contains, rule.value_offset, rule.search_rows)
        if rule.mode == "regex" and rule.pattern:
            return _from_regex(g, rule.pattern, rule.search_rows)
    except Exception:  # noqa: BLE001 -- dating a run must never break parsing it
        return None
    return None
