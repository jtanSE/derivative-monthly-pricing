"""Synthetic broker-statement generator for robustness/drift testing.

Real broker files differ month to month in ways that MUST NOT change our output
(extra columns, reordered columns, a preamble of letterhead rows, jittered
numbers) and in one way that MUST be caught (a column the spec depends on gets
renamed or dropped). This builds statements exercising both, deterministically
from a seed so failures reproduce.

A ``FormatProfile`` describes one broker's layout as an ordered list of logical
fields and their header text; ``make_spec`` derives the matching ParserSpec, and
``generate_statement`` writes a realistic .xlsx with:

  * a few preamble / banner rows before the header (letterhead noise),
  * the profile's columns PLUS ``extra_cols`` random noise columns with
    plausible headers and junk values, in a seed-shuffled order (the trade-ref
    anchor is kept in the first few columns, where every real statement puts it
    and where the engine looks),
  * ``n_rows`` data rows whose trade refs are STABLE across months (so months
    join on ref) while numeric values jitter with the seed,
  * optional ``rename`` / ``drop`` to simulate a broker changing the format.

``generate_statement`` returns the path and a ``truth`` map
{ref: {field: value}} of exactly what it wrote, so a test can assert the parsed
output reproduces it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import openpyxl

from derivative_pricing.adaptive.spec import ColumnRule, DirectionRule, ParserSpec

# Logical field kinds the generator knows how to synthesize.
#   ref        -> stable id (anchor column)
#   number     -> float
#   date       -> datetime.date
#   text       -> short string
#   putcall    -> 'C' / 'P'
#   direction  -> 'Long' / 'Short'
Kind = str


@dataclass(frozen=True)
class FieldSpec:
    field: str          # canonical field name (trade_ref, mtm_local, ...)
    header: str         # header text written into the sheet
    kind: Kind


@dataclass
class FormatProfile:
    broker: str
    sheet_name: str
    fields: list[FieldSpec]
    preamble: list[str] = field(default_factory=list)

    @property
    def anchor_field(self) -> FieldSpec:
        for f in self.fields:
            if f.field == "trade_ref":
                return f
        raise ValueError("profile has no trade_ref field")


# Noise-column headers. Deliberately share no substring with the field headers
# below, so they add structure without stealing a mapping (exact/shortest-match
# disambiguation is covered by the engine's own tests). Values are junk.
_NOISE_HEADERS = [
    "Trader Desk", "Book ID", "Clearing House", "CSA Reference", "Region",
    "Portfolio Code", "Confirmation Status", "Delta", "Gamma", "Vega", "Theta",
    "Settlement Days", "Internal Comment", "Legal Entity", "Coverage Officer",
    "Reset Frequency", "Collateral Flag", "Rating", "Desk Note", "Batch Seq",
]

_UNDERLYINGS = [".SPX", ".RUT", ".NDX", ".SX5E"]


def default_profile(broker: str = "Wells Fargo",
                    sheet_name: str = "Valuation") -> FormatProfile:
    """A realistic flat equity-option statement matching one enum broker with no
    saved spec, so tests can route it against an in-test spec without touching
    the real specs directory."""
    return FormatProfile(
        broker=broker,
        sheet_name=sheet_name,
        preamble=[
            f"{broker} Prime Services",
            "Client Valuation Statement",
            "This statement is confidential.",
        ],
        fields=[
            FieldSpec("trade_ref", "Trade Reference", "ref"),
            FieldSpec("underlying", "Underlying RIC", "text"),
            FieldSpec("put_call", "Option Type", "putcall"),
            FieldSpec("direction", "Buy / Sell", "direction"),
            FieldSpec("quantity", "Number of Contracts", "number"),
            FieldSpec("strike", "Strike Price", "number"),
            FieldSpec("underlying_price", "Underlying Level", "number"),
            FieldSpec("expiry", "Expiry Date", "date"),
            FieldSpec("trade_date", "Trade Date", "date"),
            FieldSpec("currency", "Currency", "text"),
            FieldSpec("mtm_local", "Mark to Market Value", "number"),
        ],
    )


def credit_suisse_profile() -> FormatProfile:
    """A NEW (spec-less) broker whose columns are named quite differently from
    the reference formats -- 'Share Number' for quantity, 'Valuation (USD)' for
    MTM, 'Right (P/C)' for call/put -- to test that onboarding can map
    unfamiliar header wording onto the canonical fields."""
    return FormatProfile(
        broker="Credit Suisse",
        sheet_name="OTC Valuations",
        preamble=["Credit Suisse AG", "OTC Equity Derivatives — Client Valuation",
                  "Indicative marks, not a confirmation."],
        fields=[
            FieldSpec("trade_ref", "Deal Reference", "ref"),
            FieldSpec("underlying", "Underlying Name", "text"),
            FieldSpec("put_call", "Right (P/C)", "putcall"),
            FieldSpec("direction", "Bought / Sold", "direction"),
            FieldSpec("quantity", "Share Number", "number"),
            FieldSpec("strike", "Strike Level", "number"),
            FieldSpec("underlying_price", "Reference Spot", "number"),
            FieldSpec("expiry", "Expiry", "date"),
            FieldSpec("trade_date", "Trade Dt", "date"),
            FieldSpec("currency", "Settlement Ccy", "text"),
            FieldSpec("mtm_local", "Valuation (USD)", "number"),
        ],
    )


def jp_morgan_profile() -> FormatProfile:
    """Another NEW broker, with a LEANER layout: no underlying-price and no
    trade-date columns (some statements omit them). Tests onboarding a format
    that is missing whole optional columns from the outset."""
    return FormatProfile(
        broker="JP Morgan",
        sheet_name="Position Detail",
        preamble=["J.P. Morgan Securities LLC", "Daily Position & Valuation Report"],
        fields=[
            FieldSpec("trade_ref", "Transaction ID", "ref"),
            FieldSpec("underlying", "Index RIC", "text"),
            FieldSpec("put_call", "Call/Put Flag", "putcall"),
            FieldSpec("direction", "Long or Short", "direction"),
            FieldSpec("quantity", "No. of Options", "number"),
            FieldSpec("strike", "Exercise Price", "number"),
            FieldSpec("expiry", "Maturity", "date"),
            FieldSpec("currency", "Currency", "text"),
            FieldSpec("mtm_local", "Mark Value", "number"),
        ],
    )


def make_spec(profile: FormatProfile) -> ParserSpec:
    """Derive the ParserSpec that parses ``profile``'s statements."""
    kind_map = {"ref": "text", "text": "text", "number": "number",
                "date": "date", "putcall": "text", "direction": "text"}
    columns = [
        ColumnRule(field=f.field, header_contains=f.header, kind=kind_map[f.kind])
        for f in profile.fields if f.field != "direction"
    ]
    direction = DirectionRule(mode="none")
    for f in profile.fields:
        if f.field == "direction":
            direction = DirectionRule(mode="column", header_contains=f.header,
                                      short_values=["short", "sell", "s"])
    return ParserSpec(
        broker=profile.broker,
        sheet_name=profile.sheet_name,
        header_anchor=profile.anchor_field.header,
        columns=columns,
        direction=direction,
    )


def _stable_ref(i: int) -> str:
    return f"SWD{100000000 + i * 7}"


def _synth_value(kind: Kind, fname: str, i: int, seed: int):
    """One realistic value for a field. A pure function of (seed, field, row):
    numerics jitter with the seed (month) but are INDEPENDENT of column order or
    noise columns, so adding noise can never perturb a field's value. Refs,
    underlyings, put/call and dates are seed-independent so months join on ref."""
    if kind == "ref":
        return _stable_ref(i)
    if kind == "text":
        return _UNDERLYINGS[i % len(_UNDERLYINGS)]  # currency handled by caller
    if kind == "putcall":
        return "P" if i % 9 == 0 else "C"
    if kind == "direction":
        return "Short" if i % 2 else "Long"
    if kind == "date":
        return date(2026, 3, 31) + timedelta(days=(i * 13) % 400)
    if kind == "number":
        local = random.Random(f"{seed}:{fname}:{i}")
        return round(local.uniform(-5_000_000, 5_000_000), 6)
    raise ValueError(kind)


def generate_statement(
    profile: FormatProfile,
    path: Path | str,
    *,
    seed: int = 0,
    n_rows: int = 40,
    extra_cols: int = 0,
    shuffle: bool = False,
    rename: dict[str, str] | None = None,
    drop: set[str] | None = None,
    missing: dict[str, float] | None = None,
) -> tuple[Path, dict[str, dict]]:
    """Write a synthetic statement; return (path, truth).

    rename:  {field_name: new_header} simulates a broker renaming a column.
    drop:    {field_name, ...} simulates a broker removing a column entirely.
    missing: {field_name: probability} leaves individual CELLS blank at random
             (a column that is present but sparsely filled). Distinct from drop
             (whole column gone). Best used on optional numeric fields; the
             ``truth`` map records None for the blanked cells.
    """
    rename = rename or {}
    drop = drop or set()
    missing = missing or {}
    # A layout RNG for noise/order only -- kept SEPARATE from field values so
    # adding noise columns or shuffling never changes a field's data.
    layout_rng = random.Random(f"{seed}:layout")

    active = [f for f in profile.fields if f.field not in drop]

    # Assemble the header layout: real field columns + noise columns.
    # Each entry is (header_text, producer) where producer(i)->cell value.
    layout: list[tuple[str, object]] = []
    truth: dict[str, dict] = {}

    for f in active:
        header = rename.get(f.field, f.header)

        def make_producer(fs: FieldSpec):
            def produce(i: int):
                if fs.kind == "text" and fs.field == "currency":
                    return "USD"
                return _synth_value(fs.kind, fs.field, i, seed)
            return produce

        layout.append((header, ("field", f, make_producer(f))))

    noise_pool = list(_NOISE_HEADERS)
    layout_rng.shuffle(noise_pool)
    for h in noise_pool[:extra_cols]:
        # noise: half numeric, half short text, independent of row identity
        is_num = layout_rng.random() < 0.5
        layout.append((h, ("noise", is_num)))

    if shuffle:
        layout_rng.shuffle(layout)
        # Keep the anchor within the first few columns (where real statements
        # put the id and where the engine's anchor scan looks).
        anchor_header = rename.get("trade_ref", profile.anchor_field.header)
        idx = next(k for k, (h, _) in enumerate(layout) if h == anchor_header)
        if idx > 3:
            swap = layout_rng.randint(0, 3)
            layout[idx], layout[swap] = layout[swap], layout[idx]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = profile.sheet_name
    for line in profile.preamble:
        ws.append([line])
    ws.append([h for h, _ in layout])

    for i in range(n_rows):
        ref = _stable_ref(i)
        row_truth: dict[str, object] = {}
        cells = []
        for _header, meta in layout:
            if meta[0] == "noise":
                is_num = meta[1]
                cells.append(round(layout_rng.uniform(0, 1000), 3) if is_num
                             else layout_rng.choice(["A", "B", "C", "n/a", "-"]))
                continue
            _, fs, produce = meta
            val = produce(i)
            # blank this cell with the field's configured probability (a present
            # but sparsely-filled column). Deterministic per (seed, field, row).
            p = missing.get(fs.field, 0.0)
            if p and random.Random(f"{seed}:miss:{fs.field}:{i}").random() < p:
                val = None
            cells.append(val)
            # record the logical truth (direction is derived downstream)
            if fs.field == "direction":
                row_truth["direction"] = None if val is None else str(val)
            else:
                row_truth[fs.field] = val
        truth[ref] = row_truth
        ws.append(cells)

    path = Path(path)
    wb.save(path)
    return path, truth
