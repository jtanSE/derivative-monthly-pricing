"""Month-end market data: index prices + FX rates.

Today this is analyst-entered (Bloomberg terminal lookups keyed in by hand,
exactly like the workbook's T:U and V:X blocks). The ``MarketData`` container
is the seam: a future Bloomberg API adapter produces the same object, so
nothing downstream changes when the API arrives.

CSV input formats (templates in data/templates/):
  fx_rates.csv      -> currency,rate_to_usd
  index_prices.csv  -> index_name,ticker,price
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from derivative_pricing.reference.indices import canonicalize_index


@dataclass
class MarketData:
    as_of: str  # YYYY-MM-DD
    fx_rates: dict[str, float] = field(default_factory=dict)  # currency -> to-USD rate
    index_prices: dict[str, float] = field(default_factory=dict)  # canonical name -> price

    def fx(self, currency: str) -> float:
        try:
            return self.fx_rates[currency.strip().upper()]
        except KeyError:
            raise KeyError(f"No FX rate for currency {currency!r} as of {self.as_of}") from None

    def index_price(self, index_name: str) -> float:
        key = canonicalize_index(index_name)
        try:
            return self.index_prices[key]
        except KeyError:
            raise KeyError(f"No month-end price for index {index_name!r} as of {self.as_of}") from None


def load_market_data(
    as_of: str,
    fx_csv: Path | str | None = None,
    index_csv: Path | str | None = None,
) -> MarketData:
    """Either input may be omitted (e.g. FX supplied but index prices sourced
    from the statements themselves)."""
    md = MarketData(as_of=as_of)
    if fx_csv is not None:
        with open(fx_csv, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                md.fx_rates[row["currency"].strip().upper()] = float(row["rate_to_usd"])
    if index_csv is not None:
        with open(index_csv, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                md.index_prices[canonicalize_index(row["index_name"])] = float(row["price"])
    return md