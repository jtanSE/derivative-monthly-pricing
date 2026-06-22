"""Format router: decide whether an incoming file is already known.

This is the dispatcher between the two paths (SYSTEMIMPLEMENTATION.md s.8):

  known file   -> run the existing spec deterministically (fast, no AI)
  unknown file -> hand off to the learning agent to propose a new spec

Recognition signature: every spec carries a ``header_anchor`` (and optionally a
``sheet_name``). A file is "known" if exactly one spec's signature is found in
it. The anchors are distinctive per broker ("BNPP's ref", "ML Ref.", "GS Ref",
"FOS TRADE ID"), so the header anchor alone identifies the format; sheet_name,
when set, must also match (it is skipped when null, e.g. GS uses a
client-specific sheet name).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from derivative_pricing.adaptive.agent import load_reference_specs
from derivative_pricing.adaptive.engine import _norm, run_spec
from derivative_pricing.adaptive.reader import Grid, load_grid
from derivative_pricing.adaptive.spec import ParserSpec
from derivative_pricing.schema import BrokerMark


def _anchor_present(grid: Grid, anchor: str) -> bool:
    a = _norm(anchor)
    for r in range(min(grid.nrows, 80)):
        for c in range(min(grid.ncols, 8)):
            if a in _norm(grid.text(r, c)):
                return True
    return False


def _signature_matches(spec: ParserSpec, grid: Grid) -> bool:
    if spec.sheet_name and _norm(spec.sheet_name) != _norm(grid.name):
        return False
    return _anchor_present(grid, spec.header_anchor)


def identify(path: Path | str, specs: list[ParserSpec] | None = None) -> list[ParserSpec]:
    """Return every known spec whose signature matches the file (usually 0 or 1)."""
    specs = specs if specs is not None else load_reference_specs()
    grid = load_grid(path)
    return [s for s in specs if _signature_matches(s, grid)]


@dataclass
class RouteResult:
    path: str
    status: str  # "known" | "unknown" | "ambiguous"
    spec: ParserSpec | None = None
    marks: list[BrokerMark] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)  # broker names if ambiguous

    @property
    def broker(self) -> str | None:
        return self.spec.broker.value if self.spec else None


def route(path: Path | str, specs: list[ParserSpec] | None = None) -> RouteResult:
    """Identify a file and, if known, parse it deterministically.

    Unknown/ambiguous files are returned with empty marks and a status flag so
    the caller can hand them to the learning agent (unknown) or a human
    (ambiguous) -- never silently mis-parsed.
    """
    path = Path(path)
    matches = identify(path, specs)
    if len(matches) == 1:
        spec = matches[0]
        return RouteResult(str(path), "known", spec=spec, marks=run_spec(spec, path))
    if len(matches) == 0:
        return RouteResult(str(path), "unknown")
    return RouteResult(str(path), "ambiguous", candidates=[s.broker.value for s in matches])


def route_folder(folder: Path | str, specs: list[ParserSpec] | None = None) -> list[RouteResult]:
    """Route every broker statement in a folder. Handles mixed formats and a
    mix of known/unknown brokers in one pass."""
    folder = Path(folder)
    specs = specs if specs is not None else load_reference_specs()
    results: list[RouteResult] = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in (".xls", ".xlsx") and not path.name.startswith("~$"):
            try:
                results.append(route(path, specs))
            except Exception as e:
                results.append(RouteResult(str(path), "unknown", candidates=[f"error: {e}"]))
    return results
