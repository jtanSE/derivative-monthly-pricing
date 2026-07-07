"""Freeze a month-end market-data snapshot from a provider.

This is the bridge between a ``MarketDataProvider`` (Bloomberg HAPI, or the
fake) and the immutable CSVs the pricing pipeline reads. It:

  1. reads a *universe* (which index securities and which FX pairs to pull),
  2. asks the provider for quotes as of the month-end,
  3. converts FX quotes to a consistent ``rate_to_usd`` (handling USD-quoted
     pairs like USDJPY by inversion),
  4. writes three files under ``data/<as-of>/``:
       - ``index.csv``    index_name,ticker,price      (load_market_data reads this)
       - ``fx.csv``       currency,rate_to_usd         (load_market_data reads this)
       - ``snapshot.json`` full lineage for every value (source, ticker, field,
                          raw quote, conversion, pull timestamp)

Immutability: an existing snapshot is never silently overwritten -- a
correction is a new pull with ``overwrite=True`` and its own timestamp, so the
prior audit trail is preserved.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from derivative_pricing.reference.bloomberg import (
    MarketDataProvider,
    SecurityRequest,
)
from derivative_pricing.reference.indices import CANONICAL_INDICES, canonicalize_index


@dataclass(frozen=True)
class IndexSecurity:
    """An index to pull: its canonical name (as it appears in broker/ALIC
    sheets) plus the Bloomberg security to request it by."""

    index_name: str
    identifier: str  # e.g. "SPX Index"
    field: str = "PX_LAST"


@dataclass(frozen=True)
class FxSecurity:
    """A currency's rate to USD.

    ``convention`` says how to turn the raw quote into rate_to_usd:
      - "direct":  pair is XXXUSD (e.g. EURUSD) -> rate_to_usd = quote
      - "inverse": pair is USDXXX (e.g. USDJPY) -> rate_to_usd = 1 / quote
    ``fixed_rate`` short-circuits the pull (USD -> 1.0)."""

    currency: str
    identifier: str | None = None  # None only when fixed_rate is set
    field: str = "PX_LAST"
    convention: str = "direct"
    fixed_rate: float | None = None


@dataclass
class MarketDataUniverse:
    indices: list[IndexSecurity] = field(default_factory=list)
    fx: list[FxSecurity] = field(default_factory=list)

    @classmethod
    def from_json(cls, path: Path | str) -> MarketDataUniverse:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            indices=[IndexSecurity(**d) for d in raw.get("indices", [])],
            fx=[FxSecurity(**d) for d in raw.get("fx", [])],
        )

    @classmethod
    def bootstrap_from_indices(cls) -> MarketDataUniverse:
        """Seed an index universe from the canonical index->ticker table.

        Indices whose ticker is unknown (None) are skipped -- they can't be
        pulled until someone supplies the Bloomberg security. USD is added as a
        fixed 1.0 rate so every book has at least its base currency.
        """
        indices = [
            IndexSecurity(index_name=name, identifier=f"{ticker} Index")
            for name, ticker in CANONICAL_INDICES.items()
            if ticker
        ]
        return cls(indices=indices, fx=[FxSecurity(currency="USD", fixed_rate=1.0)])


@dataclass
class QuoteLineage:
    """Provenance for one frozen value -- the audit record."""

    kind: str  # "index" or "fx"
    key: str  # index_name or currency
    identifier: str | None
    field: str | None
    raw_quote: float | None
    resolved_value: float | None  # price, or rate_to_usd
    convention: str | None = None
    note: str | None = None


@dataclass
class MarketDataSnapshot:
    as_of: str
    pulled_at_utc: str
    source: str
    index_prices: dict[str, float] = field(default_factory=dict)  # canonical name -> price
    fx_rates: dict[str, float] = field(default_factory=dict)  # currency -> rate_to_usd
    lineage: list[QuoteLineage] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # keys the provider had no data for


def _now_utc_iso(pulled_at: dt.datetime | None) -> str:
    stamp = pulled_at or dt.datetime.now(dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def build_snapshot(
    universe: MarketDataUniverse,
    provider: MarketDataProvider,
    as_of: str,
    *,
    source: str = "bloomberg-hapi",
    pulled_at: dt.datetime | None = None,
) -> MarketDataSnapshot:
    """Pull the universe from ``provider`` and resolve it into a snapshot.

    No files are written here (pure, testable). ``freeze_snapshot`` persists.
    """
    # One provider call for everything that needs pulling.
    to_pull: list[SecurityRequest] = []
    for ix in universe.indices:
        to_pull.append(SecurityRequest(ix.identifier, ix.field))
    for fx in universe.fx:
        if fx.fixed_rate is None and fx.identifier:
            to_pull.append(SecurityRequest(fx.identifier, fx.field))

    quotes = provider.fetch(to_pull, as_of) if to_pull else []
    by_id: dict[str, float | None] = {q.identifier: q.value for q in quotes}

    snap = MarketDataSnapshot(
        as_of=as_of, pulled_at_utc=_now_utc_iso(pulled_at), source=source
    )

    for ix in universe.indices:
        raw = by_id.get(ix.identifier)
        canon = canonicalize_index(ix.index_name)
        if raw is None:
            snap.missing.append(f"index:{ix.index_name}")
            snap.lineage.append(QuoteLineage(
                "index", canon, ix.identifier, ix.field, None, None,
                note="no data from provider"))
            continue
        snap.index_prices[canon] = raw
        snap.lineage.append(QuoteLineage(
            "index", canon, ix.identifier, ix.field, raw, raw))

    for fx in universe.fx:
        if fx.fixed_rate is not None:
            snap.fx_rates[fx.currency.upper()] = fx.fixed_rate
            snap.lineage.append(QuoteLineage(
                "fx", fx.currency.upper(), None, None, None, fx.fixed_rate,
                note="fixed rate"))
            continue
        raw = by_id.get(fx.identifier) if fx.identifier else None
        if raw is None or raw == 0:
            snap.missing.append(f"fx:{fx.currency}")
            snap.lineage.append(QuoteLineage(
                "fx", fx.currency.upper(), fx.identifier, fx.field, raw, None,
                convention=fx.convention, note="no/zero data from provider"))
            continue
        rate = raw if fx.convention == "direct" else 1.0 / raw
        snap.fx_rates[fx.currency.upper()] = rate
        snap.lineage.append(QuoteLineage(
            "fx", fx.currency.upper(), fx.identifier, fx.field, raw, rate,
            convention=fx.convention))

    return snap


def freeze_snapshot(snap: MarketDataSnapshot, out_dir: Path | str,
                    *, overwrite: bool = False) -> Path:
    """Write index.csv, fx.csv and snapshot.json. Refuses to clobber an
    existing frozen snapshot unless ``overwrite=True`` (immutability guard)."""
    out = Path(out_dir)
    snapshot_json = out / "snapshot.json"
    if snapshot_json.exists() and not overwrite:
        raise FileExistsError(
            f"A frozen snapshot already exists at {snapshot_json}. Corrections "
            "must be a fresh pull with overwrite=True so the prior audit trail "
            "is preserved deliberately, not by accident."
        )
    out.mkdir(parents=True, exist_ok=True)

    with open(out / "index.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["index_name", "ticker", "price"])
        for lin in snap.lineage:
            if lin.kind == "index" and lin.resolved_value is not None:
                w.writerow([lin.key, lin.identifier, lin.resolved_value])

    with open(out / "fx.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["currency", "rate_to_usd"])
        for ccy, rate in snap.fx_rates.items():
            w.writerow([ccy, rate])

    payload = {
        "as_of": snap.as_of,
        "pulled_at_utc": snap.pulled_at_utc,
        "source": snap.source,
        "index_prices": snap.index_prices,
        "fx_rates": snap.fx_rates,
        "missing": snap.missing,
        "lineage": [asdict(lin) for lin in snap.lineage],
    }
    snapshot_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return snapshot_json
