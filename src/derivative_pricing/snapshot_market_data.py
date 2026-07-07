"""Freeze a month-end market-data snapshot (index prices + FX) from Bloomberg.

This is a SEPARATE step from the pricing run, on purpose: the snapshot is pulled
once, frozen, and then the deterministic month-end run reads the frozen files.
Bloomberg is never in the pricing path (auditability / reproducibility).

Typical use
-----------
Dry run with no credentials (canned data, proves the wiring):
    uv run python -m derivative_pricing.snapshot_market_data \
        --as-of 2026-03-31 --out data/2026-03-31 --fake

Real pull (credentials from the environment / secrets manager):
    BLOOMBERG_HAPI_CLIENT_ID=...  BLOOMBERG_HAPI_CLIENT_SECRET=... \
    BLOOMBERG_HAPI_CATALOG=...  uv run python -m derivative_pricing.snapshot_market_data \
        --as-of 2026-03-31 --out data/2026-03-31 --universe data/templates/market_universe.json

Then price against the frozen snapshot:
    uv run python -m derivative_pricing.run_month <statements> --as-of 2026-03-31 \
        --fx data/2026-03-31/fx.csv --index data/2026-03-31/index.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

from derivative_pricing.reference.market_snapshot import (
    MarketDataUniverse,
    build_snapshot,
    freeze_snapshot,
)


def _load_universe(path: str | None) -> MarketDataUniverse:
    if path:
        return MarketDataUniverse.from_json(path)
    # No universe file: seed indices from the canonical table (USD-only FX).
    return MarketDataUniverse.bootstrap_from_indices()


def _make_provider(use_fake: bool, universe: MarketDataUniverse):
    if use_fake:
        # Deterministic canned quotes for every security in the universe, so a
        # dry run produces a complete snapshot with no network / credentials.
        from derivative_pricing.reference.bloomberg import FakeProvider

        canned: dict[str, float] = {}
        for i, ix in enumerate(universe.indices):
            canned[ix.identifier] = 1000.0 + i  # arbitrary but stable
        for fx in universe.fx:
            if fx.identifier:
                canned[fx.identifier] = 1.1
        return FakeProvider(canned)

    from derivative_pricing.reference.bloomberg import (
        BloombergHapiConfig,
        BloombergHapiProvider,
    )

    return BloombergHapiProvider(BloombergHapiConfig.from_env())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Freeze a month-end market-data snapshot")
    ap.add_argument("--as-of", required=True, help="month-end date, e.g. 2026-03-31")
    ap.add_argument("--out", required=True, help="snapshot output dir, e.g. data/2026-03-31")
    ap.add_argument("--universe", help="universe JSON (default: seed from canonical indices)")
    ap.add_argument("--fake", action="store_true",
                    help="use canned data instead of Bloomberg (no credentials needed)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an existing frozen snapshot (a deliberate correction)")
    args = ap.parse_args(argv)

    universe = _load_universe(args.universe)
    provider = _make_provider(args.fake, universe)
    source = "fake" if args.fake else "bloomberg-hapi"

    snap = build_snapshot(universe, provider, args.as_of, source=source)
    path = freeze_snapshot(snap, args.out, overwrite=args.overwrite)

    print(f"Snapshot frozen: {path}")
    print(f"  as-of:       {snap.as_of}")
    print(f"  source:      {snap.source}")
    print(f"  pulled (UTC):{snap.pulled_at_utc}")
    print(f"  indices:     {len(snap.index_prices)} priced")
    print(f"  fx:          {len(snap.fx_rates)} rates")
    if snap.missing:
        print(f"  MISSING ({len(snap.missing)}) - no data pulled, review before pricing:")
        for key in snap.missing:
            print(f"    [MISS] {key}")


if __name__ == "__main__":
    main()
