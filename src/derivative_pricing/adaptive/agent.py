"""AI parser-proposer agent.

Workflow (SYSTEMIMPLEMENTATION.md section 8):
  fingerprint(file)         -> compact structural digest of an unknown statement
  build_prompt(fp, examples)-> few-shot prompt (approved specs as examples)
  propose_spec(...)         -> calls Claude to draft a ParserSpec
                               (via ANTHROPIC_API_KEY, else the local claude CLI)
  validate_spec(spec, file) -> deterministic quality gate on the drafted spec
  save_spec(spec)           -> persist an approved spec so the broker is "known"
  onboard_file(file)        -> end-to-end: known? else draft -> validate -> save

The LLM is used only to DRAFT the spec (structure), once per new format. The
drafted spec is then reviewed by a human and executed deterministically by
engine.run_spec every month. The model never reads monthly numbers. A new
broker becomes permanently known only after save_spec writes its spec to disk.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.adaptive.reader import Grid, load_grid
from derivative_pricing.adaptive.spec import ParserSpec
from derivative_pricing.schema import Broker

SPECS_DIR = Path(__file__).parent / "specs"

# Kill switch for LLM-assisted onboarding. The deterministic monthly pipeline
# (route -> run_spec -> pricing) never calls the model, so it is unaffected by
# this flag either way. Only propose_spec / onboard_file consult it.
AI_ONBOARDING_FLAG = "AI_ONBOARDING_ENABLED"
_DISABLED_VALUES = {"false", "0", "no", "off"}

# Spend cap: a second, automatic kill switch layered on the manual on/off one.
# When AI_ONBOARDING_BUDGET_USD is set to a positive dollar figure, drafting is
# refused once cumulative recorded spend reaches it. Like AI_ONBOARDING_ENABLED
# this is read from the process environment (set at deploy time, e.g. in
# docker-compose from the host env / a secrets manager), NOT from anything a UI
# user can edit -- see SECURITY-NOTES.md. Unset / blank / <= 0 = no cap.
AI_ONBOARDING_BUDGET_FLAG = "AI_ONBOARDING_BUDGET_USD"

# All-time token/cost ledger, owned by the backend (this module) so the budget
# gate reads the same counter that records spend. The UI only displays it.
USAGE_TOTALS_PATH = Path(__file__).resolve().parents[3] / "data" / "usage_totals.json"


def ai_onboarding_enabled(env: dict[str, str] | None = None) -> bool:
    """True unless AI_ONBOARDING_ENABLED is set to a falsey value
    (false / 0 / no / off, case-insensitive). Unset or empty -> enabled, so the
    container default keeps onboarding available; an operator disables it by
    setting the flag explicitly."""
    e = env if env is not None else os.environ
    raw = (e.get(AI_ONBOARDING_FLAG) or "").strip().lower()
    return raw not in _DISABLED_VALUES


def onboarding_budget_usd(env: dict[str, str] | None = None) -> float | None:
    """Dollar spend cap from AI_ONBOARDING_BUDGET_USD, or None for no cap.
    Blank / unparseable / <= 0 -> None (treated as unset), so a malformed value
    never silently becomes a tiny cap that wedges onboarding."""
    e = env if env is not None else os.environ
    raw = (e.get(AI_ONBOARDING_BUDGET_FLAG) or "").strip()
    if not raw:
        return None
    try:
        cap = float(raw)
    except ValueError:
        return None
    return cap if cap > 0 else None


def load_usage_totals(path: Path | None = None) -> dict:
    """All-time {total_tokens, total_cost_usd}. Missing/unreadable -> zeros
    (a fresh install has spent nothing)."""
    p = path or USAGE_TOTALS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"total_tokens": 0, "total_cost_usd": 0.0}
    return {
        "total_tokens": int(data.get("total_tokens", 0) or 0),
        "total_cost_usd": float(data.get("total_cost_usd", 0.0) or 0.0),
    }


def _write_usage_totals(totals: dict, path: Path | None = None) -> None:
    # Atomic write: a concurrent Streamlit run (or a crash mid-write) must never
    # leave a half-written counter that reads back as low spend and reopens the
    # budget. Write a temp file, then os.replace (atomic on POSIX and Windows).
    p = path or USAGE_TOTALS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(totals, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def record_usage(usage: dict | None, path: Path | None = None) -> dict:
    """Fold one drafting call's usage into the persistent all-time ledger and
    return the updated totals. No-op (returns current totals) for a None or
    zero-token usage. This is the single place spend is persisted."""
    totals = load_usage_totals(path)
    if usage and usage.get("total_tokens"):
        totals["total_tokens"] += int(usage["total_tokens"])
        totals["total_cost_usd"] += float(usage.get("cost_usd", 0.0) or 0.0)
        _write_usage_totals(totals, path)
    return totals


def budget_status(env: dict[str, str] | None = None,
                  path: Path | None = None) -> tuple[bool, float | None, float]:
    """(exhausted, cap, spent) for the spend kill switch. ``exhausted`` is
    False whenever no cap is configured. ``spent`` is the live all-time
    dollar total; ``cap`` is None when uncapped."""
    cap = onboarding_budget_usd(env)
    spent = load_usage_totals(path)["total_cost_usd"]
    return (cap is not None and spent >= cap, cap, spent)


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
    allowed_brokers = ", ".join(b.value for b in Broker)
    return f"""You map broker option-statement spreadsheets into a ParserSpec.

A ParserSpec is JSON with: broker, sheet_name, header_anchor (text that
identifies the header row), header_requires, columns (each {{field,
header_contains, kind}}), direction (mode: column|sign_of|none),
banner_prefixes, repeated_headers, split_legs (true when ref-less leg rows
under a net parent row carry the per-leg direction/quantity/strike/mtm).
header_requires is a list of extra header substrings used ONLY when the
statement stacks several tables that share the same header_anchor (e.g. a
"Trade Ref." column appears in a bond-forward table AND an equity-options
table). List a few column headers unique to the table you want (e.g.
["Quantity", "Put / Call", "Strike Rate"]) and the engine will lock onto the
one header row containing all of them. Leave it empty ([]) for ordinary
single-table statements.
broker MUST be exactly one of: {allowed_brokers} — copy the spelling verbatim,
even if the statement's letterhead styles the name differently.
Required fields are trade_ref and mtm_local. mtm_local is the trade's
mark-to-market / valuation column the client consumes, extracted exactly AS
REPORTED by the broker — never flip signs in a spec; sign conventions are
applied downstream.

Here are approved specs for other brokers:

{ex}

Now produce ONLY a JSON ParserSpec (no prose) for this new statement:

{fp.to_json()}
"""


def _find_claude_cli() -> str | None:
    """Locate the Claude Code CLI: PATH first, then the VS Code extension's
    bundled native binary (how it ships on this desktop)."""
    import shutil

    exe = shutil.which("claude")
    if exe:
        return exe
    ext_dir = Path.home() / ".vscode" / "extensions"
    candidates = list(ext_dir.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"))
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    return None


def drafting_backend() -> str | None:
    """Which LLM backend can draft specs right now: 'api' | 'cli' | None.

    The API key always wins when present, so provisioning ANTHROPIC_API_KEY
    later needs no code change; the locally authenticated Claude Code CLI is
    the zero-config fallback. The AI-onboarding kill switch is checked by the
    callers, not here."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if _find_claude_cli():
        return "cli"
    return None


# Per-MTok USD pricing for the API path (input, output). The CLI path reports
# its cost directly, so this table is only consulted for direct API calls.
# Source: platform.claude.com pricing (cached 2026-06); cache reads bill ~0.1x
# input, cache writes ~1.25x input.
API_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Usage of the most recent LLM drafting call (either backend), for cost
# reporting in the UI. Shape: {backend, input_tokens, output_tokens,
# cache_creation_input_tokens, cache_read_input_tokens, total_tokens, cost_usd}
_LAST_USAGE: dict | None = None


def last_draft_usage() -> dict | None:
    return _LAST_USAGE


def _api_cost_usd(model: str, usage) -> float:
    inp, out = API_PRICING_PER_MTOK.get(model, API_PRICING_PER_MTOK["claude-opus-4-8"])
    cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        usage.input_tokens * inp
        + cache_w * inp * 1.25
        + cache_r * inp * 0.10
        + usage.output_tokens * out
    ) / 1_000_000


def _canonical_broker(name: str) -> str:
    """Map a model-drafted broker name onto the closed Broker enum when they
    differ only by punctuation/spacing/case ("J.P. Morgan" -> "JP Morgan").
    Genuinely new counterparties fall through unchanged and still fail
    validation loudly, which is the correct outcome."""
    key = "".join(ch for ch in name.casefold() if ch.isalnum())
    for b in Broker:
        if "".join(ch for ch in b.value.casefold() if ch.isalnum()) == key:
            return b.value
    return name


def _extract_json(text: str) -> str:
    """Models sometimes wrap the JSON in fences or a line of prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _draft_via_api(prompt: str, model: str) -> str:
    global _LAST_USAGE
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed (uv add anthropic)") from e
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    u = msg.usage
    cache_w = getattr(u, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(u, "cache_read_input_tokens", 0) or 0
    _LAST_USAGE = {
        "backend": "api",
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": cache_w,
        "cache_read_input_tokens": cache_r,
        "total_tokens": u.input_tokens + u.output_tokens + cache_w + cache_r,
        "cost_usd": round(_api_cost_usd(model, u), 6),
    }
    return msg.content[0].text


def _draft_via_cli(prompt: str) -> str:
    """Draft via the Claude Code CLI in print mode (same prompt/output contract
    as the API path). The CLI uses its own configured model, so the ``model``
    argument does not apply on this path.

    ``--output-format json`` wraps the answer in an envelope that carries
    ``usage`` token counts and ``total_cost_usd``, which we record for the
    UI's token/cost meter."""
    global _LAST_USAGE
    import subprocess

    exe = _find_claude_cli()
    if exe is None:
        raise RuntimeError("claude CLI not found")
    proc = subprocess.run(
        [exe, "-p", "--output-format", "json"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"claude CLI drafting failed: {(proc.stderr or '<no output>').strip()[:300]}"
        )
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI drafting failed: {str(envelope.get('result'))[:300]}")
    u = envelope.get("usage") or {}
    tokens = {k: int(u.get(k, 0) or 0) for k in (
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    )}
    _LAST_USAGE = {
        "backend": "cli",
        **tokens,
        "total_tokens": sum(tokens.values()),
        "cost_usd": round(float(envelope.get("total_cost_usd") or 0.0), 6),
    }
    return envelope.get("result") or ""


def propose_spec(path: Path | str, model: str = "claude-opus-4-8") -> ParserSpec:
    """Draft a spec with Claude. Requires the AI-onboarding kill switch to be
    on and a drafting backend (ANTHROPIC_API_KEY, else the local Claude Code
    CLI); raises otherwise with guidance to use the manual/human path."""
    global _LAST_USAGE
    _LAST_USAGE = None  # never report a previous draft's tokens for this one
    if not ai_onboarding_enabled():
        raise RuntimeError(
            f"AI onboarding is disabled ({AI_ONBOARDING_FLAG}=false): no model "
            "call will be made. Re-enable it, or hand-author specs/<broker>.json "
            "and validate with validate_spec()."
        )
    exhausted, cap, spent = budget_status()
    if exhausted:
        raise RuntimeError(
            f"AI onboarding budget reached: ${spent:,.2f} spent of the "
            f"${cap:,.2f} cap ({AI_ONBOARDING_BUDGET_FLAG}); no model call will be "
            "made. Raise the cap in the deployment environment, or hand-author "
            "specs/<broker>.json and validate with validate_spec()."
        )
    backend = drafting_backend()
    if backend is None:
        raise RuntimeError(
            "No drafting backend: ANTHROPIC_API_KEY not set and no claude CLI "
            "found. Build the spec manually (a human/LLM authoring "
            "specs/<broker>.json) and validate with validate_spec()."
        )
    fp = fingerprint(path)
    prompt = build_prompt(fp, load_reference_specs())
    text = _draft_via_api(prompt, model) if backend == "api" else _draft_via_cli(prompt)
    # Persist spend immediately -- BEFORE any parse/validation below can raise --
    # so the budget counter always reflects tokens actually billed, and this is
    # the one chokepoint that writes the ledger (onboard_file goes through here).
    record_usage(_LAST_USAGE)
    raw = json.loads(_extract_json(text))
    if isinstance(raw, dict) and isinstance(raw.get("broker"), str):
        raw["broker"] = _canonical_broker(raw["broker"])
    return ParserSpec.model_validate(raw)


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
        header_row = _find_header_row(grid, spec.header_anchor, spec.header_requires)
        cols = _map_columns(grid, header_row, spec.columns)
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


def update_spec(spec: ParserSpec, specs_dir: Path | None = None) -> tuple[Path, Path | None]:
    """Replace an existing broker's spec in place, preserving the old version.

    Used when a KNOWN broker's statement format drifts and a human confirms the
    re-drafted spec. The previous spec is copied to a timestamped file under
    ``specs/backups/`` first (recoverable, and never picked up by
    ``load_reference_specs`` -- that globs ``*.json`` in the specs dir only, not
    the backups subdir). Returns (written_path, backup_path|None)."""
    specs_dir = specs_dir or SPECS_DIR
    specs_dir.mkdir(parents=True, exist_ok=True)
    spec.require()
    path = specs_dir / f"{_slug(spec.broker.value)}.json"

    backup: Path | None = None
    if path.exists():
        backups_dir = specs_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = backups_dir / f"{_slug(spec.broker.value)}-{stamp}.json"
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path, backup


@dataclass
class OnboardResult:
    status: str  # "already_known" | "proposed" | "saved" | "needs_key" | "rejected"
                 # | "disabled" | "budget_exhausted" | "unreadable"
    broker: str | None = None
    spec: ParserSpec | None = None
    validation: ValidationReport | None = None
    saved_path: str | None = None
    message: str = ""
    # Token/cost usage of the drafting call (None when no model was invoked).
    usage: dict | None = None


def onboard_file(path: Path | str, approve: bool = False,
                 force: bool = False) -> OnboardResult:
    """End-to-end onboarding of a (possibly new) broker statement.

    - Already known -> no-op, reports the matched broker (unless ``force``).
    - Unknown -> draft a spec (needs a drafting backend: ANTHROPIC_API_KEY or
      the local claude CLI), validate it, and -- only
      if ``approve=True`` and validation passes -- persist it so the broker
      becomes known. Without approval the proposed spec is returned for human
      review but NOT saved (institutional control: no auto-trust).

    ``force=True`` re-drafts even for an already-known broker: this is the
    "the format changed, propose an updated spec" path. It returns a proposed
    spec for review; the caller adopts it via ``update_spec`` (which overwrites
    the old spec, keeping a backup) rather than ``save_spec``.
    """
    # local import avoids a circular import (router imports agent)
    from derivative_pricing.adaptive.router import identify

    # An unreadable file (password-protected, corrupt) fails here, before any
    # recognition or drafting is possible. Report it as its own outcome: no
    # amount of spec drafting can open an encrypted workbook, and fingerprint()
    # would raise on the same read a moment later -- after the caller had been
    # told a model call was worth trying.
    try:
        matches = identify(path)
    except Exception as e:  # noqa: BLE001
        return OnboardResult(
            "unreadable",
            message=(f"Could not read this file: {e}. It cannot be onboarded until it "
                     "opens -- supply the password, or ask the broker to re-send it "
                     "unprotected."),
        )
    if matches and not force:
        # Known broker -- but confirm the file still MATCHES the saved spec's
        # layout. A broker can keep its recognizable header yet rename/drop a
        # column the spec depends on; that must surface here, not silently.
        from derivative_pricing.adaptive.drift import check_spec_drift

        msg = "A spec already covers this format."
        try:  # informational only -- a read hiccup must not break recognition
            drift = check_spec_drift(matches[0], path)
            if drift.missing_required or drift.missing_optional:
                msg = (f"A spec already covers this format, but the layout changed since "
                       f"it was authored -- {drift.summary()} Re-onboard to update the spec.")
        except Exception:  # noqa: BLE001
            pass
        return OnboardResult("already_known", broker=matches[0].broker.value,
                             spec=matches[0], message=msg)

    # Kill switch: unknown format, but the AI path is turned off. Deterministic
    # parsing of already-known brokers above is never affected.
    if not ai_onboarding_enabled():
        return OnboardResult(
            "disabled",
            message=(
                f"Unknown format, but AI onboarding is disabled "
                f"({AI_ONBOARDING_FLAG}=false). Re-enable it, or hand-author "
                "specs/<broker>.json then re-run."
            ),
        )

    # Spend kill switch: unknown format, but the budget is used up. Same
    # fail-closed default as the manual switch -- no model call is made.
    exhausted, cap, spent = budget_status()
    if exhausted:
        return OnboardResult(
            "budget_exhausted",
            message=(
                f"Unknown format, but the AI onboarding budget is used up: "
                f"${spent:,.2f} of ${cap:,.2f} ({AI_ONBOARDING_BUDGET_FLAG}). "
                "Raise the cap in the deployment environment, or hand-author "
                "specs/<broker>.json then re-run."
            ),
        )

    if drafting_backend() is None:
        return OnboardResult(
            "needs_key",
            message="Unknown format. Set ANTHROPIC_API_KEY (or install the claude "
                    "CLI) to auto-draft a spec, or hand-author "
                    "specs/<broker>.json then re-run.",
        )

    try:
        spec = propose_spec(path)
    except Exception as e:
        # Most common cause: the drafted spec names a counterparty outside the
        # closed Broker enum (schema.py). Make the remedy explicit instead of
        # surfacing a raw validation traceback. Tokens may still have been
        # spent on the failed draft, so usage is attached here too.
        return OnboardResult(
            "rejected",
            usage=last_draft_usage(),
            message=(
                f"Spec drafting failed: {e}. If this is a NEW counterparty, add it to "
                "schema.Broker and (if it reports dealer-perspective MTM) to "
                "parsers.base.BROKER_MTM_SIGN, then re-run onboarding."
            ),
        )
    usage = last_draft_usage()
    report = validate_spec(spec, path)
    if not report.ok:
        return OnboardResult("rejected", broker=spec.broker.value, spec=spec, validation=report,
                             usage=usage, message=f"Validation failed: {report.issues}")
    if not approve:
        return OnboardResult("proposed", broker=spec.broker.value, spec=spec, validation=report,
                             usage=usage,
                             message="Spec drafted and validated. Review, then onboard with approve=True.")
    saved = save_spec(spec)
    return OnboardResult("saved", broker=spec.broker.value, spec=spec, validation=report,
                         usage=usage,
                         saved_path=str(saved), message=f"Saved {saved.name}; broker now known.")
