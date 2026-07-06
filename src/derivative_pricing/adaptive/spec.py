"""Declarative parser specification.

A ParserSpec is *data, not code*: it says where each field lives in a broker
statement, and the generic engine (engine.py) executes it. This is the format
the AI proposer emits for a new broker, and what a human reviews/approves.

A spec is intentionally constrained so it cannot do anything dangerous: it can
only point at sheets/columns and pick from a fixed set of value transforms and
sign rules. Worst case it points at the wrong column, which validation and
human review catch.

Expressive enough for the three reference formats:
- BNP  : flat table, header anchor "BNPP's ref", Long/Short direction column
- GS   : flat table, header anchor "GS Ref", Buy/Sell direction, banner rows
- BAML : repeated per-deal headers, two legs, signed Position as direction
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from derivative_pricing.schema import Broker

ValueKind = Literal["text", "number", "date"]


class ColumnRule(BaseModel):
    """How to find one field's column and read its value."""

    model_config = {"extra": "forbid"}  # typo'd directives must fail loudly

    field: str  # canonical field name (trade_ref, mtm_local, quantity, ...)
    header_contains: str  # case-insensitive substring of the column header
    kind: ValueKind = "text"


class DirectionRule(BaseModel):
    """How to derive Long/Short for a row.

    mode:
      - "column": read a column; values in ``short_values`` mean Short.
      - "sign_of": Short when the named numeric field is negative.
      - "none": always Long (single-direction statements).
    """

    model_config = {"extra": "forbid"}

    mode: Literal["column", "sign_of", "none"] = "none"
    header_contains: str | None = None  # for mode="column"
    short_values: list[str] = Field(default_factory=lambda: ["short", "sell", "s"])
    sign_of_field: str | None = None  # for mode="sign_of" (e.g. "quantity")


class ParserSpec(BaseModel):
    # NOTE on signs: specs extract values AS-REPORTED by the broker — a spec
    # never flips MTM signs. The single source of sign truth is
    # parsers.base.BROKER_MTM_SIGN (Barclays/Nomura report dealer-perspective),
    # applied downstream by the output layer. This prevents double-flips.

    model_config = {"extra": "forbid"}  # typo'd directives must fail loudly

    broker: Broker
    # Sheet selection: exact name, else first sheet.
    sheet_name: str | None = None
    # Header row detection: the row whose first few cells contain this text.
    header_anchor: str
    # Required + optional column rules. trade_ref and mtm_local are mandatory.
    columns: list[ColumnRule]
    direction: DirectionRule = Field(default_factory=DirectionRule)
    # Rows whose trade_ref (lowercased) starts with any of these are banners.
    banner_prefixes: list[str] = Field(default_factory=list)
    # Header rows repeat through the body (BAML deal blocks).
    repeated_headers: bool = False

    def column(self, field: str) -> ColumnRule | None:
        return next((c for c in self.columns if c.field == field), None)

    def require(self) -> None:
        fields = {c.field for c in self.columns}
        missing = {"trade_ref", "mtm_local"} - fields
        if missing:
            raise ValueError(f"spec for {self.broker.value} missing columns: {missing}")
