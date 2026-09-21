"""Index-name canonicalization.

Verified data-quality issues this solves (plan section 1.5):
- The same index appears with different capitalization in different broker
  sections ("S&P DYNAMIC INTRADAY TCA" vs "S&P Dynamic Intraday TCA").
- Lookups must be deterministic regardless of stray whitespace/case.

The Barclays-section placeholder issue ("Barclays" written instead of the
actual custom index name) is intentionally NOT silently fixed here: it needs
trade-level data to resolve, so it surfaces as its raw value and the matching
layer decides. Canonical spellings below are seeded from the workbook's own
month-end index table (columns V/W/X).
"""

from __future__ import annotations

# canonical name -> known ticker (from the workbook V column where available)
CANONICAL_INDICES: dict[str, str | None] = {
    "S&P 500": "SPX",
    "Russell 2000": "RTY",
    "NASDAQ Night Owl": "NIGHTOWL",
    "S&P Dynamic Intraday TCA": "SPFDYNI",
    "S&P MARC 5% Excess Return": "SPMARC5P",
    "Dynamic Balanced Asset 10": "MQIDBA10",
    "Strategic Dynamic Balanced Asset 8": "MQISDB08",
    "Multi-Asset Dynamic Managed 5": "MQIMDM05",
    "US Strategic Balanced Asset 8": "MQIMSB08",
    "US Multi-Asset Risk Managed 5": "MQIMRM05",
    "US Balanced Asset 10": None,
    "US Daily Risk Managed 12": None,
    "US Multi-Asset Diversified 5": None,
    "Barclays": None,  # placeholder used in the Barclays workbook section
}

_BY_FOLDED = {name.casefold(): name for name in CANONICAL_INDICES}

# Values brokers write into an "underlying" column that name the PRODUCT, not
# the index. Verified: Nomura's Aug-2026 statement puts "Compound" in
# ``underlying`` on exactly the 12 rows whose ``productType`` is also
# "Compound". Passing that through produces a row whose Index reads "Compound",
# which then fails every index-price lookup while looking like a real name --
# worse than a blank, because a blank is visibly missing.
_NOT_INDEX_NAMES = frozenset({
    "compound",
    "exoticoption",
    "exotic option",
    "option",
    "notapplicable",
    "not applicable",
    "n/a",
    "na",
    "none",
})


def is_index_name(raw: str | None) -> bool:
    """False for blanks and for product-type values that are not index names."""
    cleaned = " ".join(str(raw or "").split())
    return bool(cleaned) and cleaned.casefold() not in _NOT_INDEX_NAMES


def canonicalize_index(raw: str) -> str:
    """Return the canonical spelling for a raw index name.

    Unknown names are returned cleaned (trimmed) but otherwise untouched --
    never guessed -- so new indices flow through visibly rather than being
    silently renamed. Product-type values (see _NOT_INDEX_NAMES) are rejected to
    an empty string: they are not indices, and leaving them in makes a
    populated-looking Index column that can never price.
    """
    cleaned = " ".join(raw.split())
    if not is_index_name(cleaned):
        return ""
    return _BY_FOLDED.get(cleaned.casefold(), cleaned)