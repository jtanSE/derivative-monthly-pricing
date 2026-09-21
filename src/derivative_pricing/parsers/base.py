"""Broker parser layer: shared contract, registry, and per-broker conventions.

Every broker statement parser must emit ``list[BrokerMark]`` (schema.BrokerMark):
at minimum (broker, trade_ref, mtm_local) plus lineage. Broker quirks live
here as data/config -- the core engines never special-case a broker.

Rule F3 (verified empirically from the workbook): the analyst's sheet computes
internal MTM (col I) from the raw broker mark (col M). For Barclays and Nomura
it flips the sign (``I = -M``); for every other broker it passes through
(``I = +M``). These two brokers report MTM from the dealer's perspective,
opposite to ALIC's position.

Evidence (March 2026 test file, 100% consistent within each broker):
  Barclays  431/431 rows flipped
  Nomura    297/297 rows flipped
  all others             pass-through
Reconfirm against additional months as more statements arrive (plan Q2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from derivative_pricing.schema import Broker, BrokerMark

# F3: multiplier applied to broker-reported MTM to get the internal view.
BROKER_MTM_SIGN: dict[Broker, int] = {broker: 1 for broker in Broker}
BROKER_MTM_SIGN[Broker.BARCLAYS] = -1
BROKER_MTM_SIGN[Broker.NOMURA] = -1


def apply_sign_convention(broker: Broker, value: float) -> float:
    """Convert a broker-reported value into the internal (client-view) value
    under rule F3. Dealer-perspective brokers (Barclays, Nomura) report BOTH
    their MTM and their position size negated relative to the client's view, so
    the same multiplier converts either quantity — verified against the ALIC
    sheet, where Nomura's shares are exactly -exchangedNotional1amount."""
    return BROKER_MTM_SIGN[broker] * value


class StatementParser(Protocol):
    """Contract for a raw broker statement parser (plan Phase 2)."""

    broker: Broker

    def parse(self, path: Path) -> list[BrokerMark]: ...


# Registry: broker -> parser factory. Raw-statement parsers register here as
# they are built (Phase 2, blocked on example files -- plan Q2).
PARSER_REGISTRY: dict[Broker, Callable[[], StatementParser]] = {}


def register_parser(broker: Broker):
    def deco(cls):
        cls.broker = broker
        PARSER_REGISTRY[broker] = cls
        return cls

    return deco