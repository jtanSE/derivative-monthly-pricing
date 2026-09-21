"""Per-row anomaly flags for parsed statements.

Parsing can succeed and still yield a value that is obviously wrong for its
peers -- a strike of 1.0 sitting among strikes of ~5,000, a negative price. This
flags those rows so a human eye lands on them, WITHOUT rejecting them (the number
parsed fine; it just looks off).

Detection is deliberately conservative to avoid crying wolf. The signal we want
is an *order-of-magnitude* break -- a strike of 1.0 sitting among strikes of
~5,000 -- not ordinary spread:

  * We screen the STRIKE only. Strike is structurally anchored to the
    underlying's price level, so a strike far off its group's scale is a real
    red flag (a scale/parse error). Per-option PRICE is deliberately NOT
    screened: it legitimately spans orders of magnitude within one underlying
    (deep-OTM vs ITM premium; short legs carry a negative price), so magnitude
    tests there are almost all false positives.
  * Strikes are compared only WITHIN the same equity-index / underlying group --
    SPX strikes (~5,000) and RUT strikes (~2,000) must not flag each other.
  * The peer scale is the median of the group's POSITIVE strikes; a strike flags
    only when it differs from that median by more than ``_RATIO`` (~50x, well
    over an order of magnitude). Ordinary dispersion within a group (even a wide
    20x range) does not flag -- only clear magnitude breaks do.
  * A strictly NEGATIVE strike flags. A zero is left alone: some brokers
    legitimately report 0 as "no strike" for a whole class of trades, and
    flagging a repeated convention is just noise.

The output is one list of human-readable reasons per input row (empty == clean),
so the UI can show a marker and explain why in a detail panel.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass

# A value >_RATIO x or <1/_RATIO x the group's positive median is a magnitude
# break. ~50x sits comfortably above legitimate within-underlying spread while
# still catching a 1.0-among-5,000s.
_RATIO = 50.0
# Need at least this many positive peers before "normal" is well-defined.
_MIN_N = 3


@dataclass
class Cell:
    """One parsed row's screenable fields. ``group`` is the scale bucket (equity
    index / underlying) within which its ``strike`` is compared to peers."""

    group: str | None
    strike: float | None = None


def _positive_median(values: list[float | None]) -> float | None:
    pos = [v for v in values if v is not None and v > 0]
    return statistics.median(pos) if len(pos) >= _MIN_N else None


def robust_outlier_mask(
    values: list[float | None],
    *,
    ratio: float = _RATIO,
) -> list[bool]:
    """Boolean mask over ``values`` (None never flags). A value flags when it is
    strictly negative, or when it differs from the positive-peer median by more
    than ``ratio`` in either direction. Zeros and sub-threshold spread never
    flag. With fewer than _MIN_N positive peers, only negatives flag."""
    mask = [False] * len(values)
    med = _positive_median(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        if v < 0:
            mask[i] = True
        elif med is not None and v > 0:
            r = v / med if v >= med else med / v
            if r > ratio:
                mask[i] = True
    return mask


def row_warnings(cells: list[Cell]) -> list[list[str]]:
    """One list of reason strings per input row (empty == no anomaly)."""
    reasons: list[list[str]] = [[] for _ in cells]

    groups: dict[str | None, list[int]] = defaultdict(list)
    for i, c in enumerate(cells):
        groups[c.group].append(i)

    for idxs in groups.values():
        vals = [cells[i].strike for i in idxs]
        mask = robust_outlier_mask(vals)
        med = _positive_median(vals)
        for pos, i in enumerate(idxs):
            if not mask[pos]:
                continue
            v = cells[i].strike
            if v < 0:
                reasons[i].append(f"Strike {v:,.2f} is negative")
            elif med is not None:
                reasons[i].append(
                    f"Strike {v:,.2f} is far from its peers (median {med:,.2f})")

    return [list(dict.fromkeys(r)) for r in reasons]
