"""Matching engine: join broker marks to the trade population.

MVP strategy (plan Phase 4): exact join on trade reference
(Trade.counterparty_trade_id == BrokerMark.trade_ref). Composite-key
fallbacks (cusip; counterparty+index+expiry+strike+shares) will be layered in
once a real traders export confirms the join keys (plan Q1).

Hard requirement: mismatches are FLAGGED, never silently dropped or
overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from derivative_pricing.schema import BrokerMark, Trade


@dataclass
class MatchOutcome:
    matched: list[tuple[Trade, BrokerMark]] = field(default_factory=list)
    unmatched_trades: list[Trade] = field(default_factory=list)
    unmatched_marks: list[BrokerMark] = field(default_factory=list)
    # Trade refs that appear on more than one mark (e.g. double-legged trades
    # reported as separate rows) need explicit pairing rules (plan Q5).
    ambiguous_marks: dict[str, list[BrokerMark]] = field(default_factory=dict)


def match_marks_to_trades(trades: list[Trade], marks: list[BrokerMark]) -> MatchOutcome:
    outcome = MatchOutcome()

    marks_by_ref: dict[str, list[BrokerMark]] = {}
    for mark in marks:
        marks_by_ref.setdefault(mark.trade_ref, []).append(mark)

    claimed: set[str] = set()
    for trade in trades:
        ref = trade.counterparty_trade_id
        candidates = marks_by_ref.get(ref, [])
        if len(candidates) == 1:
            outcome.matched.append((trade, candidates[0]))
            claimed.add(ref)
        elif len(candidates) > 1:
            outcome.ambiguous_marks[ref] = candidates
            claimed.add(ref)
        else:
            outcome.unmatched_trades.append(trade)

    for ref, candidates in marks_by_ref.items():
        if ref not in claimed:
            outcome.unmatched_marks.extend(candidates)

    return outcome