"""AI parser-proposer agent.

Workflow (SYSTEMIMPLEMENTATION.md section 8):
  fingerprint(file)         -> compact structural digest of an unknown statement
  build_prompt(fp, examples)-> few-shot prompt (approved specs as examples)
  propose_spec(...)         -> calls Claude to draft a ParserSpec (needs API key)
  validate_spec(spec, file) -> deterministic quality gate on the drafted spec
  save_spec(spec)           -> persist an approved spec so the broker is "known"
  onboard_file(file)        -> end-to-end: known? else draft -> validate -> save

The LLM is used only to DRAFT the spec (structure), once per new format. The
drafted spec is then reviewed by a human and executed deterministically by
engine.run_spec every month. The model never reads monthly numbers. A new
broker becomes permanently known only after save_spec writes its spec to disk.
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
    # utf-8-sig: tolerate a BOM from Windows editors/tools; strict JSON rejects it
    return ParserSpec.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


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
direction (mode: column|sign_of|none), banner_prefixes, repeated_headers.
Required fields are trade_ref and mtm_local. mtm_local is the trade's
mark-to-market / valuation column the client consumes, extracted exactly AS
REPORTED by the broker — never flip signs in a spec; sign conventions are
applied downstream.

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

    # Column-collision check: a drafted spec that binds mtm_local to the same
    # physical column as strike/quantity/price is a mis-mapping (e.g. an LLM
    # pointing "mtm_local" at the Strike column), even though it parses fine.
    from derivative_pricing.adaptive.engine import _find_header_row, _map_columns
    from derivative_pricing.adaptive.reader import load_grid

    try:
        grid = load_grid(path)
        cols = _map_columns(grid, _find_header_row(grid, spec.header_anchor), spec.columns)
        mtm_col = cols.get("mtm_local")
        for other in ("strike", "quantity", "underlying_price", "mid_price"):
            if mtm_col is not None and cols.get(other) == mtm_col:
                issues.append(f"mtm_local resolves to the same column as {other!r} — mis-mapped spec")
    except Exception:
        pass  # header/grid problems are already reported via run_spec above

    return ValidationReport(
        ok=not issues,
        rows_parsed=len(marks),
        distinct_refs=distinct,
        pct_with_quantity=round(with_qty, 3),
        pct_with_strike=round(with_strike, 3),
        issues=issues,
    )


def _slug(broker_name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in broker_name.lower()).strip("_")


def save_spec(spec: ParserSpec, filename: str | None = None, specs_dir: Path | None = None) -> Path:
    """Persist an approved spec into the specs directory, making the broker
    permanently 'known' to the router. Filename defaults to a slug of the
    broker name. Refuses to silently overwrite an existing spec."""
    specs_dir = specs_dir or SPECS_DIR
    specs_dir.mkdir(parents=True, exist_ok=True)
    path = specs_dir / (filename or f"{_slug(spec.broker.value)}.json")
    if path.exists():
        raise FileExistsError(f"spec already exists: {path.name} (delete/rename to replace)")
    spec.require()
    path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


@dataclass
class OnboardResult:
    status: str  # "already_known" | "proposed" | "saved" | "needs_key" | "rejected"
    broker: str | None = None
    spec: ParserSpec | None = None
    validation: ValidationReport | None = None
    saved_path: str | None = None
    message: str = ""


def onboard_file(path: Path | str, approve: bool = False) -> OnboardResult:
    """End-to-end onboarding of a (possibly new) broker statement.

    - Already known -> no-op, reports the matched broker.
    - Unknown -> draft a spec (needs ANTHROPIC_API_KEY), validate it, and -- only
      if ``approve=True`` and validation passes -- persist it so the broker
      becomes known. Without approval the proposed spec is returned for human
      review but NOT saved (institutional control: no auto-trust).
    """
    # local import avoids a circular import (router imports agent)
    from derivative_pricing.adaptive.router import identify

    matches = identify(path)
    if matches:
        return OnboardResult("already_known", broker=matches[0].broker.value,
                             spec=matches[0], message="A spec already covers this format.")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return OnboardResult(
            "needs_key",
            message="Unknown format. Set ANTHROPIC_API_KEY to auto-draft a spec, "
                    "or hand-author specs/<broker>.json then re-run.",
        )

    try:
        spec = propose_spec(path)
    except Exception as e:
        # Most common cause: the drafted spec names a counterparty outside the
        # closed Broker enum (schema.py). Make the remedy explicit instead of
        # surfacing a raw validation traceback.
        return OnboardResult(
            "rejected",
            message=(
                f"Spec drafting failed: {e}. If this is a NEW counterparty, add it to "
                "schema.Broker and (if it reports dealer-perspective MTM) to "
                "parsers.base.BROKER_MTM_SIGN, then re-run onboarding."
            ),
        )
    report = validate_spec(spec, path)
    if not report.ok:
        return OnboardResult("rejected", broker=spec.broker.value, spec=spec,
                             validation=report, message=f"Validation failed: {report.issues}")
    if not approve:
        return OnboardResult("proposed", broker=spec.broker.value, spec=spec, validation=report,
                             message="Spec drafted and validated. Review, then onboard with approve=True.")
    saved = save_spec(spec)
    return OnboardResult("saved", broker=spec.broker.value, spec=spec, validation=report,
                         saved_path=str(saved), message=f"Saved {saved.name}; broker now known.")
