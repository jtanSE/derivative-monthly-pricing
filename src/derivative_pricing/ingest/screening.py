"""Entry screening: check every incoming mark for repeats and maturity.

Applied as marks are ingested (run_month, the UI combined view), this is an
operations control so nothing is silently double-counted or priced past its
maturity:

- **repeat**  — an entry identical to one already accepted this run (same
  broker, trade ref, MTM, strike, quantity). The repeat is dropped from the
  priced set and reported. Note: a double-legged deal (e.g. BAML) has two
  *different* legs under one trade ref — those are NOT repeats because their
  strike/quantity/MTM differ.
- **matured** — maturity/expiry date is on or before the as-of month-end. It
  is FLAGGED but kept in the output: a matured-but-unsettled trade can still
  appear in broker records and matter for MTM. True archiving (matured AND
  settled) needs the traders-DB settlement date and happens in the canonical
  live-trades layer.

``existing_keys`` lets a caller screen against an already-persisted dataset
(the future canonical table), so a mark repeated across months is caught too.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from derivative_pricing.schema import BrokerMark

_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%m/%d/%Y", "%d/%m/%Y")


def parse_date(value) -> dt.date | None:
    """Parse the mixed date strings brokers emit (ISO and '31-Dec-2026')."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    # last resort: leading ISO date
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def content_key(m: BrokerMark) -> tuple:
    """Identity of an entry for repeat detection — full content, so genuine
    legs of a double-legged deal (same ref, different strike/qty) don't collide."""
    e = m.extra
    return (
        m.broker.value,
        m.trade_ref,
        round(m.mtm_local, 2),
        e.get("strike"),
        e.get("quantity"),
    )


@dataclass
class EntryFlag:
    broker: str
    trade_ref: str
    kind: str  # "repeat" | "matured" | "unknown_maturity"
    detail: str


@dataclass
class ScreenResult:
    as_of: dt.date | None
    kept: list[BrokerMark] = field(default_factory=list)  # priced set (repeats removed)
    repeats: list[EntryFlag] = field(default_factory=list)
    matured: list[EntryFlag] = field(default_factory=list)
    unknown_maturity: list[EntryFlag] = field(default_factory=list)

    @property
    def n_screened(self) -> int:
        return len(self.kept) + len(self.repeats)


def screen_marks(
    marks: list[BrokerMark],
    as_of: str | dt.date,
    existing_keys: set[tuple] | None = None,
) -> ScreenResult:
    as_of_date = parse_date(as_of)
    seen: set[tuple] = set(existing_keys) if existing_keys else set()
    res = ScreenResult(as_of=as_of_date)

    for m in marks:
        key = content_key(m)
        if key in seen:
            res.repeats.append(EntryFlag(m.broker.value, m.trade_ref, "repeat",
                                         "identical to an entry already loaded"))
            continue
        seen.add(key)

        mat = parse_date(m.extra.get("expiry"))
        if mat is None:
            res.unknown_maturity.append(EntryFlag(m.broker.value, m.trade_ref,
                                                  "unknown_maturity",
                                                  f"unparseable maturity {m.extra.get('expiry')!r}"))
        elif as_of_date is not None and mat <= as_of_date:
            res.matured.append(EntryFlag(m.broker.value, m.trade_ref, "matured",
                                         f"matured {mat.isoformat()} <= as-of {as_of_date.isoformat()}"))

        res.kept.append(m)  # matured entries are kept (flagged, not dropped)

    return res
