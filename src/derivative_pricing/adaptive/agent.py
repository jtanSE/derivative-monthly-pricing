"""AI parser-proposer agent.

Workflow (SYSTEMIMPLEMENTATION.md section 8):
  fingerprint(file)         -> compact structural digest of an unknown statement
  build_prompt(fp, examples)-> few-shot prompt (approved specs as examples)
  propose_spec(...)         -> calls Claude to draft a ParserSpec (needs API key)
  validate_spec(spec, file) -> deterministic quality gate on the drafted spec

The LLM is used only to DRAFT the spec (structure), once per new format. The
drafted spec is then reviewed by a human and executed deterministically by
engine.run_spec every month. The model never reads monthly numbers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.adaptive.reader import Grid, load_grid
from derivative_pricing.adaptive.spec import ParserSpec

SPECS_DIR = Path(__file__).parent / "specs"


def load_spec(path: Path | str) -> ParserSpec:
    return ParserSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_reference_specs() -> list[ParserSpec]:
    return [load_spec(p) for p in sorted(SPECS_DIR.glob("*.json"))]


@dataclass
class Fingerprint:
    """Compact, model-friendly description of a statement's structure."""

    file_name: str
    sheets: list[str]
    chosen_sheet: str
    header_candidates: list[dict]  # [{row, cells:[...]}]
    sample_rows: list[dict]        # [{row, cells:[...]}]

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, default=str)


def _row_cells(grid: Grid, r, max_c=30) -> list[str]:
    return [grid.text(r, c) for c in range(min(grid.ncols, max_c))]


def fingerprint(path: Path | str, max_rows: int = 25) -> Fingerprint:
    """Build a structural digest: the first rows of the first sheet, flagging
    likely header rows (rows with many distinct non-empty text cells)."""
    path = Path(path)
    grid = load_grid(path)

    header_candidates, sample_rows = [], []
    for r in range(min(grid.nrows, max_rows)):
        cells = _row_cells(grid, r)
        nonempty = [x for x in cells if x]
        texty = [x for x in nonempty if not x.replace(".", "").replace("-", "").isdigit()]
        # header-ish: many text cells, few pure numbers
        if len(texty) >= 5 and len(nonempty) >= 5:
            header_candidates.append({"row": r + 1, "cells": cells})
        sample_rows.append({"row": r + 1, "cells": cells})

    return Fingerprint(
        file_name=path.name,
        sheets=[grid.name],
        chosen_sheet=grid.name,
        header_candidates=header_candidates,
        sample_rows=sample_rows,
    )


def build_prompt(fp: Fingerprint, examples: list[ParserSpec]) -> str:
    ex = "\n\n".join(
        f"EXAMPLE SPEC ({s.broker.value}):\n{s.model_dump_json(indent=2)}"
        for s in examples
    )
    return f"""You map broker option-statement spreadsheets into a ParserSpec.

A ParserSpec is JSON with: broker, sheet_name, header_anchor (text that
identifies the header row), columns (each {{field, header_contains, kind}}),
direction (mode: column|sign_of|none), mtm_sign (1 or -1), banner_prefixes,
repeated_headers. Required fields are trade_ref and mtm_local. mtm_local is the
trade's mark-to-market / valuation column the client consumes.

Here are approved specs for other brokers:

{ex}

Now produce ONLY a JSON ParserSpec (no prose) for this new statement:

{fp.to_json()}
"""


def propose_spec(path: Path | str, model: str = "claude-opus-4-8") -> ParserSpec:
    """Draft a spec with Claude. Requires ANTHROPIC_API_KEY; raises otherwise
    with guidance to use the manual/human path."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set: cannot call the model. Build the spec "
            "manually (a human/LLM authoring specs/<broker>.json) and validate "
            "with validate_spec()."
        )
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed (uv add anthropic)") from e

    fp = fingerprint(path)
    prompt = build_prompt(fp, load_reference_specs())
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    return ParserSpec.model_validate_json(text)


@dataclass
class ValidationReport:
    ok: bool
    rows_parsed: int
    distinct_refs: int
    pct_with_quantity: float
    pct_with_strike: float
    issues: list[str] = field(default_factory=list)


def validate_spec(spec: ParserSpec, path: Path | str) -> ValidationReport:
    """Deterministic quality gate on a drafted spec before human approval."""
    issues: list[str] = []
    try:
        marks = run_spec(spec, path)
    except Exception as e:
        return ValidationReport(False, 0, 0, 0.0, 0.0, [f"engine error: {e}"])

    if not marks:
        issues.append("no rows parsed")
    n = len(marks) or 1
    with_qty = sum(1 for m in marks if m.extra.get("quantity") is not None) / n
    with_strike = sum(1 for m in marks if m.extra.get("strike") is not None) / n
    distinct = len({m.trade_ref for m in marks})
    if with_qty < 0.5:
        issues.append(f"low quantity coverage ({with_qty:.0%})")

    return ValidationReport(
        ok=not issues,
        rows_parsed=len(marks),
        distinct_refs=distinct,
        pct_with_quantity=round(with_qty, 3),
        pct_with_strike=round(with_strike, 3),
        issues=issues,
    )
