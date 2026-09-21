"""Manual trade-exclusion list.

Some trades the analyst deliberately keeps OUT of the ALIC Call Options sheet
even though they are in the broker file (verified: Barclays Feb 2026 drops
EDR31750137EU / EDR31810103EU, which are structurally indistinguishable from
the ~119 similar trades she keeps). Nothing in the statement marks these, so
the exclusion is a human decision recorded here — the parser-layer analogue of
her manual overrides. Excluded trades are removed from the sheet and its totals
but never lost: they are listed back to the user, with the reason, and can be
restored.

Stored as JSON keyed by trade reference so it survives restarts and is easy to
diff/review:  {"<ref>": {"broker": ..., "note": ..., "at": "<iso>"}}.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "data" / "exclusions.json"


def _path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else _DEFAULT_PATH


def load_exclusions(path: Path | str | None = None) -> dict[str, dict]:
    """ref -> {broker, note, at}. Empty dict when no file / unreadable."""
    p = _path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_excluded(ref: str | None, exclusions: dict[str, dict]) -> bool:
    return ref is not None and str(ref) in exclusions


def exclude(ref: str, broker: str | None = None, note: str = "",
            path: Path | str | None = None) -> dict[str, dict]:
    """Add a trade ref to the exclusion list; returns the updated map."""
    ex = load_exclusions(path)
    ex[str(ref)] = {
        "broker": broker,
        "note": note,
        "at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    _save(ex, path)
    return ex


def restore(ref: str, path: Path | str | None = None) -> dict[str, dict]:
    """Remove a trade ref from the exclusion list; returns the updated map."""
    ex = load_exclusions(path)
    ex.pop(str(ref), None)
    _save(ex, path)
    return ex


def _save(exclusions: dict[str, dict], path: Path | str | None = None) -> None:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(exclusions, indent=2, sort_keys=True), encoding="utf-8")
