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
    # Secondary column to read when the primary is empty for a row. Barclays
    # books its .BXIIGTBB structured trades with an empty Quantity and the share
    # count under Notional, while vanilla trades have Quantity populated and
    # Notional = dollar notional; the fallback fires only on the empty-Quantity
    # rows, so the two never collide.
    fallback_header_contains: str | None = None
    # When the value comes from the fallback column, take the position SIGN from
    # that value itself (dealer-signed) rather than the direction column — the
    # Notional column is already signed, unlike the unsigned Quantity column.
    fallback_is_signed: bool = False


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


class RowFilter(BaseModel):
    """Drop data rows whose value in a named column matches an excluded value.

    For statements that mix non-option products into the same sheet as the
    options we price (e.g. Nomura interleaves ``ForwardBondTrade`` rows among
    its options): those rows carry a ref and a valuation, so the data-row
    heuristic would sum them, inflating the totals. This drops them by product
    type. Empty (the default) keeps every row.
    """

    model_config = {"extra": "forbid"}

    header_contains: str  # column whose value is tested
    exclude_values: list[str] = Field(default_factory=list)  # case-insensitive


class ValuationDateRule(BaseModel):
    """Where the statement records the date it values positions AS OF.

    Every broker puts this somewhere different, so three modes are needed:
      - "column": a per-row column (Nomura ``valuationDate``, HSBC ``MtM date COB``)
      - "label":  a labelled cell in the preamble, value ``value_offset`` cells
                  to the right (GS ``Valuation Date:`` | ``Aug 31, 2026``)
      - "regex":  buried in prose; the FIRST capture group is the date text.
                  A capture group is required rather than optional because
                  statements can quote two dates -- BNP says "indicative
                  valuation as of 01 Sep 2026, based on the Close of business
                  31 Aug 2026", and the close-of-business date is the one that
                  matches the marks. A bare match would silently take the wrong one.

    Optional: a broker whose statement carries no date at all simply omits this,
    and the caller falls back to the user-supplied as-of.
    """

    model_config = {"extra": "forbid"}

    mode: Literal["column", "label", "regex"]
    header_contains: str | None = None  # mode="column"
    label_contains: str | None = None  # mode="label"
    value_offset: int = 1  # mode="label": cells right of the label
    pattern: str | None = None  # mode="regex": needs one capture group
    # How far into the sheet to scan for a label/prose match. The preamble sits
    # above the header row in every reference format, but Barclays' is ~22 rows
    # deep and its header is at row 47, so this needs headroom.
    search_rows: int = 60


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
    # Disambiguator for statements that stack several tables sharing the same
    # header_anchor (e.g. Barclays: Bond Forward, Equity Options, and Other
    # Equity Derivatives tables all label their key column "Trade Ref."). When
    # set, the engine skips anchor-matching header rows until it finds one whose
    # cells (across ALL columns, not just the first few) contain every string
    # here -- letting a spec target "the Trade Ref table that also has Quantity
    # and Put / Call". Empty (the default) keeps the first anchor row, so every
    # existing single-table spec is unaffected.
    header_requires: list[str] = Field(default_factory=list)
    # Required + optional column rules. trade_ref and mtm_local are mandatory.
    columns: list[ColumnRule]
    direction: DirectionRule = Field(default_factory=DirectionRule)
    # Rows whose trade_ref (lowercased) starts with any of these are banners.
    banner_prefixes: list[str] = Field(default_factory=list)
    # Header rows repeat through the body (BAML deal blocks).
    repeated_headers: bool = False
    # Optional: drop data rows by a column value (e.g. non-option products mixed
    # into an options sheet). None (default) keeps every row.
    row_filter: RowFilter | None = None
    # Several tables stacked under one header_anchor (Barclays: Equity Options +
    # Other Equity Derivatives + a bond-forward table). When true, every table
    # whose columns include trade_ref + mtm_local is parsed, columns mapped per
    # table. False (default) parses only the first matching header.
    multi_table: bool = False
    # Multi-leg "Split" trades (BNP): a parent row carries ref/dates/underlying
    # and the NET mtm, followed by ref-less leg rows carrying direction/qty/
    # strike/mtm per leg. When true, the engine emits one mark per leg (with
    # the parent's ref and context) and drops the net parent row, matching how
    # the analyst's ALIC sheet records these trades (two rows, same ref).
    split_legs: bool = False
    # Optional: where the statement states its own valuation/as-of date, so a
    # run can be dated from the files instead of a hand-typed date. None means
    # "this format doesn't say", not "today".
    valuation_date: ValuationDateRule | None = None

    def column(self, field: str) -> ColumnRule | None:
        return next((c for c in self.columns if c.field == field), None)

    def require(self) -> None:
        fields = {c.field for c in self.columns}
        missing = {"trade_ref", "mtm_local"} - fields
        if missing:
            raise ValueError(f"spec for {self.broker.value} missing columns: {missing}")
