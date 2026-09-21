"""Format-drift detection: did a known broker's statement change month to month?

The engine maps columns by header *name*, so a broker can add columns, reorder
them, or pad the sheet with noise and the output is unchanged -- the spec still
resolves each field to the same header. What breaks output silently is a broker
*renaming* or *removing* a column the spec depends on: the field stops
resolving, and (for optional fields) the value just disappears from every row
with no error.

``check_spec_drift`` makes that visible. It resolves every column a spec
declares against a specific file and reports, per field, whether it still binds
and to which header. Two months of the same format produce the same
``layout_signature`` (extra/reordered/noise columns don't affect it); a renamed
or dropped column changes the signature and shows up in ``compare_layouts``.

This is the deterministic "has the format changed?" check used by the monthly
run (as a non-fatal warning) and by onboarding when a file is already known.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from derivative_pricing.adaptive.engine import _find_header_row, _map_columns, _norm
from derivative_pricing.adaptive.reader import load_grid
from derivative_pricing.adaptive.spec import ParserSpec

# Fields the engine cannot run without (see engine.run_spec). Missing either of
# these is hard drift: the file will not parse at all.
REQUIRED_FIELDS = ("trade_ref", "mtm_local")


@dataclass(frozen=True)
class ColumnStatus:
    """Whether one declared field still binds to a header, and to which one."""

    field: str
    header_contains: str      # what the spec looks for
    resolved: bool            # did it find a matching header this month?
    matched_header: str | None  # the actual header text it bound to (if any)


@dataclass
class DriftReport:
    broker: str
    file: str
    header_found: bool
    header_row: int | None
    columns: list[ColumnStatus]

    @property
    def missing_required(self) -> list[str]:
        return [c.field for c in self.columns
                if c.field in REQUIRED_FIELDS and not c.resolved]

    @property
    def missing_optional(self) -> list[str]:
        return [c.field for c in self.columns
                if c.field not in REQUIRED_FIELDS and not c.resolved]

    @property
    def ok(self) -> bool:
        """The file still parses: header located and both required fields bind.
        Missing OPTIONAL fields do not fail this -- they parse to None -- but
        they DO change the layout signature so a human is told."""
        return self.header_found and not self.missing_required

    @property
    def layout_signature(self) -> tuple[tuple[str, str], ...]:
        """Stable, order-independent fingerprint of how fields map to headers.
        Identical across months of the same format regardless of extra columns,
        column order, or row data; changes iff a field binds to a different
        header (rename) or stops binding (drop). This is the equality test for
        'did the format stay the same?'."""
        return tuple(sorted(
            (c.field, _norm(c.matched_header or "")) for c in self.columns
        ))

    def summary(self) -> str:
        if not self.header_found:
            return (f"{self.broker}: header anchor not found in {self.file} "
                    "-- format changed or wrong file.")
        parts = []
        if self.missing_required:
            parts.append("MISSING REQUIRED " + ", ".join(self.missing_required))
        if self.missing_optional:
            parts.append("missing optional " + ", ".join(self.missing_optional))
        if not parts:
            return f"{self.broker}: layout intact ({len(self.columns)} fields resolved)."
        return f"{self.broker}: {'; '.join(parts)}."


def check_spec_drift(spec: ParserSpec, path: Path | str) -> DriftReport:
    """Resolve every field a spec declares against ``path`` and report which
    still bind. Never raises for a missing header -- an absent anchor is itself
    the strongest drift signal and is reported via ``header_found=False``."""
    path = Path(path)
    grid = load_grid(path)
    try:
        header_row = _find_header_row(grid, spec.header_anchor, spec.header_requires)
    except ValueError:
        return DriftReport(
            broker=spec.broker.value, file=path.name, header_found=False,
            header_row=None,
            columns=[ColumnStatus(c.field, c.header_contains, False, None)
                     for c in spec.columns],
        )

    cols = _map_columns(grid, header_row, spec.columns)
    statuses = [
        ColumnStatus(
            field=c.field,
            header_contains=c.header_contains,
            resolved=c.field in cols,
            matched_header=grid.text(header_row, cols[c.field]) if c.field in cols else None,
        )
        for c in spec.columns
    ]
    return DriftReport(
        broker=spec.broker.value, file=path.name, header_found=True,
        header_row=header_row, columns=statuses,
    )


def compare_layouts(baseline: DriftReport, current: DriftReport) -> list[str]:
    """Human-readable differences between two months' layouts for the same spec.
    Empty list == no drift (extra/reordered/noise columns are ignored by
    construction). Non-empty == a field that used to bind now binds differently
    or not at all (or vice versa) -- i.e. the broker changed the statement."""
    diffs: list[str] = []
    if baseline.header_found != current.header_found:
        state = "found" if current.header_found else "NOT found"
        diffs.append(f"header anchor now {state} (was "
                     f"{'found' if baseline.header_found else 'NOT found'})")

    base = {f: h for f, h in baseline.layout_signature}
    curr = {f: h for f, h in current.layout_signature}
    for field in sorted(set(base) | set(curr)):
        b, c = base.get(field), curr.get(field)
        if b == c:
            continue
        if b and not c:
            diffs.append(f"{field}: header {b!r} no longer resolves (column renamed or dropped)")
        elif c and not b:
            diffs.append(f"{field}: now resolves to {c!r} (was unresolved)")
        else:
            diffs.append(f"{field}: header changed {b!r} -> {c!r}")
    return diffs
