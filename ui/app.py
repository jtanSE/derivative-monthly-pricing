"""Drag-and-drop interface for parsing broker statements.

Run:  uv run --group ui streamlit run ui/app.py

Two modes:
- Single statement: drop one file, see how it parses.
- Month-end combined: drop ALL broker files, see the ALIC-shaped sheet
  (per-broker sections, F7 subtotals, F8 grand total) in one place + download.

The actual files are parsed via the router -- never screenshots.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from derivative_pricing.adaptive.agent import (
    budget_status,
    load_reference_specs,
    load_usage_totals,
    onboard_file,
    save_spec,
    update_spec,
)
from derivative_pricing.adaptive.drift import check_spec_drift
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.adaptive.router import identify
from derivative_pricing.adaptive.statement_date import statement_date
from derivative_pricing.anomaly import Cell, row_warnings
from derivative_pricing.ingest import screen_marks
from derivative_pricing.output import (
    build_rows_from_marks,
    split_marks_by_option_type,
    valuation_review,
    write_call_options_sectioned_xlsx,
)
from derivative_pricing.output.exclusions import (
    exclude as exclude_ref,
    is_excluded,
    load_exclusions,
    restore as restore_ref,
)
from derivative_pricing.recon import section_totals

st.set_page_config(page_title="Derivative Month-End Valuation", layout="wide")
# One id per script execution (i.e. per drag/drop or click) — used by the
# token meter to group usage into "runs".
st.session_state["run_id"] = uuid.uuid4().hex

st.markdown(
    """
    <style>
      [data-testid="stFileUploaderDropzone"] {
          border: 1.5px dashed rgba(11, 11, 11, 0.22);
          border-radius: 12px;
          background: #fcfcfb;
      }
      .block-container { padding-top: 2.2rem; max-width: 1500px; }
      footer { visibility: hidden; }

      /* --- Stat tiles -------------------------------------------------------
         Labels read as quiet field names, not headings. Values keep the font's
         proportional figures: tabular-nums gives every digit a zero's width,
         which makes a short figure look loose at display size. */
      [data-testid="stMetricValue"] {
          font-size: 1.5rem;
          font-weight: 600;
          letter-spacing: -0.015em;
      }
      [data-testid="stMetricLabel"] p {
          font-size: 0.72rem;
          font-weight: 600;
          letter-spacing: 0.055em;
          text-transform: uppercase;
          color: rgba(11, 11, 11, 0.55);
      }

      /* --- Tables -----------------------------------------------------------
         Figures in a column must align vertically, so these DO get tabular
         figures -- the opposite of the stat tiles above. */
      [data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }

      /* --- Section headings -------------------------------------------------
         The broker sections use h4; tighten them so a long page of sections
         reads as a ledger rather than a stack of titles. */
      .block-container h4 {
          font-size: 1.05rem;
          font-weight: 600;
          letter-spacing: -0.01em;
          padding-top: 0.1rem;
      }

      /* --- Tabs -------------------------------------------------------------
         Default Streamlit tabs are low-contrast until selected, which makes the
         two-step workflow hard to see from across a room. */
      button[data-baseweb="tab"] { font-weight: 500; }
      button[data-baseweb="tab"][aria-selected="true"] { font-weight: 650; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Derivative Month-End Valuation")
st.caption("Broker statements in, the month-end valuation sheet out. Known formats "
           "parse on drop; an unfamiliar one is drafted for your approval before it "
           "is ever used. Figures are read from the statement files themselves.")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Trade-ref search: the month tab records its matches here each run; the
# sidebar (rendered last) reads them back. Reset up front so a run that never
# reaches the month tab can't show stale results.
st.session_state["trade_search_hits"] = []
st.session_state["trade_search_put_hits"] = 0

_HIGHLIGHT = "background-color: rgba(42, 120, 214, 0.18)"  # search-hit (blue)
_ANOMALY = "background-color: rgba(250, 178, 25, 0.16)"    # possible-anomaly (amber)
_FLAG_COL = "⚠"  # leading marker column; set on flagged rows, blank otherwise

# Scrolls the main page to the anchor rendered above the matched broker
# section. Runs from a zero-height component iframe, so it walks up to the
# parent document; retries briefly because the section may render after it.
_SNAP_JS = """
<script>
(function () {
  let tries = 20;
  const attempt = () => {
    const el = window.parent.document.getElementById("trade-hit-anchor");
    if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); }
    else if (--tries > 0) { setTimeout(attempt, 150); }
  };
  attempt();
})();
</script>
"""

# Number formatting for tables: accounting style for MTM (thousands separators,
# negatives in parentheses), localized for share counts, 4dp for unit prices.
_MARKS_COLUMNS = {
    "MTM (local)": st.column_config.NumberColumn(format="accounting"),
    "Quantity": st.column_config.NumberColumn(format="localized"),
    "Price (Ccy)": st.column_config.NumberColumn(format="%.4f"),
}
_ROWS_COLUMNS = {
    "MTM (Ccy)": st.column_config.NumberColumn(format="accounting"),
    "MTM (USD)": st.column_config.NumberColumn(format="accounting"),
    "Shares": st.column_config.NumberColumn(format="localized"),
    "Price (Ccy)": st.column_config.NumberColumn(format="%.4f"),
}


def _prev_month_end() -> date:
    """Most recent month-end on or before today.

    Used only as the date widget's fallback when nothing is dropped yet. A
    hard-coded date here is what caused August statements to be stamped March,
    so this is derived rather than written down.
    """
    today = date.today()
    return today.replace(day=1) - timedelta(days=1)


@st.cache_data(show_spinner=False)
def _stated_dates_cached(path_str: str, size: int, mtime: float) -> tuple[str, str] | None:
    """(broker, iso date) a single statement says it values as of, or None.

    Cached on path+size+mtime because Streamlit re-runs the script on every
    interaction and this reads the workbook; without the cache every click
    re-parses every file just to redraw a date field.
    """
    try:
        matches = identify(path_str)
    except Exception:  # noqa: BLE001 -- unreadable files simply have no date
        return None
    if len(matches) != 1:
        return None
    got = statement_date(matches[0], path_str)
    return (matches[0].broker.value, got) if got else None


def _stated_dates(paths: list[Path]) -> dict[str, str]:
    """broker -> stated valuation date, across the dropped statements."""
    out: dict[str, str] = {}
    for p in paths:
        try:
            stat = Path(p).stat()
        except OSError:
            continue
        hit = _stated_dates_cached(str(p), stat.st_size, stat.st_mtime)
        if hit:
            out[hit[0]] = hit[1]
    return out


def _session_tmpdir() -> Path:
    """Per-session scratch dir for uploads, created once."""
    key = "_tmpdir"
    if key not in st.session_state:
        d = Path(tempfile.mkdtemp(prefix="derivpricing-"))
        st.session_state[key] = str(d)
    d = Path(st.session_state[key])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_temp(uploaded) -> Path:
    """Persist an upload to the session scratch dir -- once per file.

    Streamlit re-runs this whole script on every interaction, so writing a fresh
    NamedTemporaryFile here put another copy of every broker statement in the
    system temp dir on every click, with nothing ever deleting them. Key the
    file on name+size and reuse it: reruns become free, downstream paths stay
    stable (so parsing can be cached against them), and one session leaves one
    copy of each statement behind instead of dozens.
    """
    cache: dict = st.session_state.setdefault("_upload_paths", {})
    key = f"{uploaded.name}:{uploaded.size}"
    hit = cache.get(key)
    if hit and Path(hit).exists():
        return Path(hit)
    # Keep the real filename: it is the only human-readable handle on which
    # broker a temp file came from when something needs investigating later.
    dest = _session_tmpdir() / f"{uuid.uuid4().hex[:8]}-{Path(uploaded.name).name}"
    dest.write_bytes(uploaded.getbuffer())
    cache[key] = str(dest)
    return dest


# ---------------------------------------------------------------------------
# Token/cost meter. Only the onboarding agent's drafting calls use an LLM
# (deterministic parsing costs zero tokens). The persistent all-time ledger is
# owned and written by the backend (agent.record_usage) so the spend kill
# switch reads the same counter it records; the UI only DISPLAYS it and tracks
# a per-session "last run" bucket for convenience.


def _record_usage(usage: dict | None) -> None:
    """Track this drafting call's tokens in the per-session 'last run' bucket
    for display. The all-time ledger is already persisted by the backend
    (agent.propose_spec -> record_usage); the UI must not write it too or spend
    would be double-counted."""
    if not usage or not usage.get("total_tokens"):
        return
    run = st.session_state.setdefault("usage_last_run", {"tokens": 0, "cost": 0.0, "run_id": None})
    # a new script run (new drag/drop) starts a fresh "last run" bucket
    if run.get("run_id") != st.session_state.get("run_id"):
        run.update({"tokens": 0, "cost": 0.0, "run_id": st.session_state.get("run_id")})
    run["tokens"] += usage["total_tokens"]
    run["cost"] += usage["cost_usd"]


def _start_over() -> None:
    """Reset the screen to a blank session: no files, default date, no search.

    Runs as a button callback, i.e. BEFORE the next script run, so every widget
    is rebuilt from scratch. A file uploader cannot be emptied by writing to its
    session-state key, so the uploaders' keys carry a generation number; bumping
    it hands Streamlit brand-new, empty widgets.

    Deliberately untouched: anything saved to disk -- approved broker formats,
    trade exclusions, and the AI cost totals. Those are decisions, not screen
    state, and must survive a reset.
    """
    gen = st.session_state.get("_upload_gen", 0) + 1
    old_tmp = st.session_state.get("_tmpdir")
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.session_state["_upload_gen"] = gen
    # The dropped statements were copied to a per-session temp folder; remove
    # them so broker data doesn't linger on disk after the user starts over.
    if old_tmp:
        shutil.rmtree(old_tmp, ignore_errors=True)


def _render_sidebar() -> None:
    with st.sidebar:
        st.button("Start over", on_click=_start_over, width="stretch",
                  help="Clears the dropped statements, the date and the search so "
                       "you can begin a fresh run. Approved broker formats and "
                       "saved exclusions are kept.")
        st.divider()
        st.subheader("Find a trade")
        query = st.text_input(
            "Trade reference ID", key="trade_search",
            placeholder="Full or partial ID",
            help="Searches the Month-end close view. The matching row is "
                 "highlighted and its broker section scrolled into view.",
        ).strip()
        if query:
            hits = st.session_state["trade_search_hits"]
            put_hits = st.session_state["trade_search_put_hits"]
            if hits:
                first_broker, first_ref = hits[0]
                if len(hits) == 1:
                    st.success(f"1 match in **{first_broker}** for **{first_ref}**")
                else:
                    st.success(f"{len(hits)} matches — first in **{first_broker}** "
                               f"for **{first_ref}**")
                # snap once per new query, not on every rerun (e.g. button clicks)
                if st.session_state.get("trade_search_last_snap") != query.casefold():
                    st.session_state["trade_search_last_snap"] = query.casefold()
                    components.html(_SNAP_JS, height=0)
            elif put_hits:
                st.info(f"{put_hits} match(es) in the **Put options** sheet — "
                        "included in the puts download, not shown on screen.")
            else:
                st.warning("No match in the loaded statements.")
        else:
            st.session_state["trade_search_last_snap"] = None

        st.divider()
        st.subheader("Known broker formats")
        known = sorted(s.broker.value for s in load_reference_specs())
        if known:
            st.markdown(" ".join(f":blue-badge[{b}]" for b in known))
        else:
            st.caption("None yet — drop a statement to onboard the first one.")
        st.caption("Statements from these brokers parse instantly. Anything else "
                   "gets a parser drafted for your review.")

        st.divider()
        st.subheader("AI usage and cost")
        totals = load_usage_totals()
        run = st.session_state.get("usage_last_run") or {"tokens": 0, "cost": 0.0}
        # plain text rows (not st.metric): the sidebar is too narrow for metric
        # tiles, which truncate instead of wrapping
        st.markdown(f"**Last run** — {run['tokens']:,} tokens · ${run['cost']:,.4f}")
        st.markdown(f"**All time** — {totals['total_tokens']:,} tokens · "
                    f"${totals['total_cost_usd']:,.2f}")
        # Spend kill switch status (read-only mirror of a server-side env var --
        # this is a display, not a control; the cap can't be changed from here).
        exhausted, cap, spent = budget_status()
        if cap is not None:
            remaining = max(cap - spent, 0.0)
            if exhausted:
                st.error(f"Budget used up — ${spent:,.2f} of ${cap:,.2f}. "
                         "Onboarding is paused until the cap is raised.")
            else:
                st.progress(min(spent / cap, 1.0),
                            text=f"Budget ${spent:,.2f} / ${cap:,.2f} "
                                 f"(${remaining:,.2f} left)")
        st.caption("AI is used only to draft a parser for a broker format the "
                   "tool has not seen before. The monthly run on known formats "
                   "uses no AI and costs nothing.")


def _halt() -> None:
    """st.stop() ends the whole script, so draw the sidebar before halting."""
    _render_sidebar()
    st.stop()


def _compact(n: float) -> str:
    """Auto-compact a large figure for a stat tile (12,900,000 -> 12.9M)."""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:,.1f}{suffix}"
    return f"{n:,.0f}"


def _unit_price(m):
    """F5 per-unit price = MTM / signed shares (short legs -> negative shares).

    MTM here is as-reported by the broker (no sign convention applied yet);
    None when the statement carries no usable quantity."""
    qty = m.extra.get("quantity")
    if not qty:
        return None
    if str(m.extra.get("direction") or "").casefold() == "short":
        qty = -qty
    return round(m.mtm_local / qty, 4)


def _marks_table(marks):
    return [
        {
            "Trade Ref": m.trade_ref,
            "MTM (local)": round(m.mtm_local, 2),
            "Ccy": m.currency,
            "Underlying": m.extra.get("underlying"),
            "Strike": m.extra.get("strike"),
            "Quantity": m.extra.get("quantity"),
            "Price (Ccy)": _unit_price(m),
            "Direction": m.extra.get("direction"),
            "Expiry": m.extra.get("expiry"),
        }
        for m in marks
    ]


def _rows_table(rows):
    return [
        {
            "Cusip": r.cusip,
            "Index": r.index,
            "Trade Reference": r.trade_reference,
            "Strike Rate": r.strike_rate,
            "Maturity Date": r.maturity_date,
            "Currency": r.currency,
            "MTM (Ccy)": None if r.mtm_local is None else round(r.mtm_local, 2),
            "MTM (USD)": None if r.mtm_usd is None else round(r.mtm_usd, 2),
            # Shares stays strictly numeric (blank when excluded) so the column
            # renders as a number; the reason lives in its own text column, so a
            # flagged row reads as "excluded pending conversion", not "zero".
            "Shares": r.shares,
            "": "⚠ review" if (r.shares is None and r.shares_review) else None,
            "Price (Ccy)": None if r.price_local is None else round(r.price_local, 4),
        }
        for r in rows
    ]


def _warnings_for_rows(rows):
    """Anomaly reasons per output row, strike scale-grouped by equity index."""
    return row_warnings([Cell(group=r.index, strike=r.strike_rate) for r in rows])


def _warnings_for_marks(marks):
    """Anomaly reasons per raw mark, strike scale-grouped by underlying."""
    return row_warnings([Cell(group=m.extra.get("underlying"),
                              strike=m.extra.get("strike")) for m in marks])


def _flagged_dataframe(table_rows, warns, *, column_config, height,
                       search_mask=None):
    """Render a table with a leading ⚠ column and a per-row tint: blue for a
    search hit, amber for a possible anomaly. Anomaly reasons are surfaced
    separately (see _anomaly_panel) since a cell can't carry a tooltip."""
    if not table_rows:
        st.dataframe(table_rows, width="stretch", hide_index=True)
        return
    rows = [{_FLAG_COL: ("⚠" if w else ""), **row}
            for row, w in zip(table_rows, warns)]
    df = pd.DataFrame(rows)
    flags = [bool(w) for w in warns]

    def _style(row, sm=search_mask, wf=flags):
        i = row.name
        if sm is not None and bool(sm.iloc[i]):
            return [_HIGHLIGHT] * len(row)
        return [_ANOMALY if wf[i] else ""] * len(row)

    cfg = {**column_config,
           _FLAG_COL: st.column_config.TextColumn(
               _FLAG_COL, width="small",
               help="Possible anomaly — parsed OK, but a value looks off vs its "
                    "peers. See the anomalies panel below the table.")}
    st.dataframe(df.style.apply(_style, axis=1), width="stretch",
                 height=height, hide_index=True, column_config=cfg)


def _anomaly_panel(label, refs, warns):
    """Expander listing each flagged row and why. Returns the flagged count."""
    flagged = [(ref, "; ".join(w)) for ref, w in zip(refs, warns) if w]
    if flagged:
        with st.expander(f"Possible anomalies in {label} ({len(flagged)})"):
            st.caption("These rows parsed fine but carry a value that looks "
                       "unusual next to its peers — worth an eyeball, not "
                       "necessarily wrong.")
            st.dataframe([{"Reference": ref, "Why flagged": why}
                          for ref, why in flagged],
                         width="stretch", hide_index=True)
    return len(flagged)


def _review_table(rows):
    """Rows whose statement quantity is a notional (ratio-strike or outlier),
    excluded from the shares subtotal until trade terms convert them."""
    return [
        {
            "Broker": r.broker,
            "Trade Reference": r.trade_reference,
            "Strike Rate": r.strike_rate,
            "Statement Qty": None,  # the raw notional isn't on the output row
            "MTM (Ccy)": None if r.mtm_local is None else round(r.mtm_local, 2),
            "Why excluded from Shares": r.shares_review,
        }
        for r in rows if r.shares_review
    ]


def _valuation_flag_panel(broker: str, broker_rows) -> int:
    """Narrow flag: rows we can neither value nor identify (no strike, no price,
    no underlying). Kept in the sheet — this is a review hint, not an exclusion.
    Returns the flagged count so the caller can badge the section."""
    flagged = [(r, valuation_review(r)) for r in broker_rows]
    flagged = [(r, why) for r, why in flagged if why]
    if flagged:
        with st.expander(f"Cannot value or identify ({len(flagged)})"):
            st.caption("No strike, no per-unit price, and no underlying — kept "
                       "(MTM stands), but there's nothing to price or identify them "
                       "by. Structured-index trades (e.g. .BXIIGTBB) are NOT here — "
                       "they're identified by their index. Consider excluding these.")
            st.dataframe(
                [{"Trade Reference": r.trade_reference,
                  "MTM (Ccy)": None if r.mtm_local is None else round(r.mtm_local, 2),
                  "Why flagged": why} for r, why in flagged],
                width="stretch", hide_index=True)
    return len(flagged)


def _exclusions_panel(broker: str, kept_rows, excluded_rows, scope: str = "") -> None:
    """Per-broker manual exclusion control + list, mirroring an analyst's manual
    overrides. Excluded trades are already out of the section, subtotal, and
    download; here the user adds to / restores from that list."""
    n = len(excluded_rows)
    label = f"Excluded from this sheet ({n})" if n else "Exclude a trade"
    with st.expander(label):
        st.caption("Trades removed from this broker's section, its F7 subtotal, and "
                   "the download — the parser-layer analogue of an analyst's manual "
                   "override. Nothing is lost: they're listed here and restorable.")

        refs = [str(r.trade_reference) for r in kept_rows if r.trade_reference]
        pick = st.selectbox("Exclude a trade reference", ["—"] + sorted(refs),
                            key=f"excl_pick::{scope}::{broker}")
        note = st.text_input("Reason (optional)", key=f"excl_note::{scope}::{broker}",
                             placeholder="e.g. analyst excludes from ALIC sheet")
        if st.button("Exclude", key=f"excl_btn::{scope}::{broker}",
                     disabled=(pick == "—"), type="primary"):
            exclude_ref(pick, broker=broker, note=note)
            st.rerun()

        if excluded_rows:
            exmap = load_exclusions()
            st.dataframe(
                [{"Trade Reference": r.trade_reference,
                  "Index": r.index,
                  "MTM (Ccy)": None if r.mtm_local is None else round(r.mtm_local, 2),
                  "Reason": (exmap.get(str(r.trade_reference)) or {}).get("note") or "—",
                  "Excluded at": (exmap.get(str(r.trade_reference)) or {}).get("at") or "—"}
                 for r in excluded_rows],
                width="stretch", hide_index=True)
            restore_pick = st.selectbox(
                "Restore a trade reference",
                ["—"] + [str(r.trade_reference) for r in excluded_rows],
                key=f"restore_pick::{scope}::{broker}")
            if st.button("Restore", key=f"restore_btn::{scope}::{broker}",
                         disabled=(restore_pick == "—")):
                restore_ref(restore_pick)
                st.rerun()


def _drift_detail(spec, path: Path, display: str) -> None:
    """Per-field 'what changed' table: which columns the saved spec still finds
    in this file and which went missing (renamed/dropped)."""
    rep = check_spec_drift(spec, path)
    with st.expander(f"What changed in the {rep.broker} layout"):
        if not rep.header_found:
            st.error("The header row itself was not found — the format changed "
                     "substantially, or this is the wrong file.")
        st.dataframe(
            [{"Field": c.field,
              "Spec looks for header": c.header_contains,
              "Still present?": "✓ yes" if c.resolved else "✗ MISSING",
              "Matched column header": c.matched_header or "—"}
             for c in rep.columns],
            width="stretch", hide_index=True,
        )


def _render_spec_proposal(display: str, path: Path, *, reonboard: bool = False) -> None:
    """Draft/preview a parser spec and let a human adopt it.

    Two modes:
      * new broker (reonboard=False) -> save_spec (refuses to overwrite);
      * format changed (reonboard=True) -> force a re-draft for an ALREADY-known
        broker and adopt via update_spec (overwrites the old spec, keeping a
        timestamped backup).

    The drafted spec is cached in session_state so button-click reruns never
    re-invoke the model (and never re-spend tokens)."""
    verb = "drafting an updated" if reonboard else "drafting a"
    cache_key = f"{'reonboard' if reonboard else 'onboard'}::{display}"
    if cache_key not in st.session_state:
        with st.spinner(f"Reading the layout of `{display}` and {verb} parser for "
                        "your review…"):
            st.session_state[cache_key] = onboard_file(path, force=reonboard)
        _record_usage(st.session_state[cache_key].usage)
    res = st.session_state[cache_key]

    if res.status == "already_known" and not reonboard:
        st.info(f"`{display}` is already covered by the **{res.broker}** spec — re-drop to parse.")
        return
    if res.status in ("needs_key", "disabled", "budget_exhausted", "rejected", "unreadable"):
        st.error(f"`{display}` — {res.message}")
        if res.validation is not None and res.validation.issues:
            st.write("Validation issues: " + "; ".join(res.validation.issues))
        if st.button("Retry", key=f"retry::{cache_key}"):
            del st.session_state[cache_key]
            st.rerun()
        return

    # status == "proposed": drafted + validated.
    v = res.validation
    badge = ":orange-badge[UPDATED SPEC — NOT YET SAVED]" if reonboard \
        else ":orange-badge[PROPOSED]"
    st.success(f"Spec {'re-' if reonboard else ''}drafted for **{res.broker}** and validated — "
               f"{v.rows_parsed} rows · {v.pct_with_quantity:.0%} with quantity · "
               f"{v.pct_with_strike:.0%} with strike.")
    marks = run_spec(res.spec, path)
    call_marks, put_marks = split_marks_by_option_type(marks)
    preview_rows = build_rows_from_marks(call_marks, fx_rates={"USD": 1.0})
    with st.container(border=True):
        st.markdown(f"#### {res.broker} — {len(preview_rows)} trades  {badge}")
        st.caption("Preview only — nothing is saved until you confirm below.")
        st.dataframe(_rows_table(preview_rows), width="stretch", height=240,
                     hide_index=True, column_config=_ROWS_COLUMNS)
        if preview_rows:
            t = section_totals(preview_rows)
            st.markdown(f"**{res.broker} subtotal (preview)** — "
                        f"MTM (Ccy): **{t.mtm_local:,.2f}** · Shares: **{t.shares:,.2f}**")
        if put_marks:
            st.caption(f"{len(put_marks)} put option(s) will route to the Put Options "
                       "sheet once saved.")
        with st.expander("Drafted parser spec (JSON)"):
            st.code(res.spec.model_dump_json(indent=2), language="json")

        if reonboard:
            label = f"Confirm the new {res.broker} layout and update the parser"
            if st.button(label, key=f"confirm::{cache_key}", type="primary"):
                saved, backup = update_spec(res.spec)
                del st.session_state[cache_key]
                bmsg = f" Previous spec backed up as `{backup.name}`." if backup else ""
                st.success(f"Updated `{saved.name}` — future {res.broker} files parse "
                           f"with the new layout.{bmsg}")
                st.rerun()
            st.caption("Not a format change? Close this without confirming and the "
                       "existing spec stays in force.")
        else:
            if st.button(f"Approve and save the {res.broker} parser",
                         key=f"approve::{cache_key}", type="primary"):
                try:
                    saved = save_spec(res.spec)
                except FileExistsError as e:
                    st.error(str(e))
                    return
                del st.session_state[cache_key]
                st.success(f"Saved `{saved.name}` — {res.broker} is now a known format.")
                st.rerun()


def _render_option_sheet(kind: str, marks, *, as_of: str, exclusions: dict,
                         primary: bool) -> None:
    """Full sectioned ALIC view for ONE option type (calls or puts): per-broker
    sections with F7 subtotals, anomaly + valuation flags, manual exclusions, a
    shares-review list, the F8 grand total, and the .xlsx download. Calls and
    puts share this identical treatment; ``kind`` is "Call" or "Put".

    ``primary`` (calls) owns the sidebar search hit-list and the scroll anchor;
    the secondary sheet still highlights its own matches in place. Widget keys
    are namespaced by ``kind`` so the same broker rendering in both sub-tabs
    doesn't collide."""
    sheet_label = f"{kind} Options"
    all_rows = build_rows_from_marks(marks, fx_rates={"USD": 1.0}) if marks else []
    if not all_rows:
        st.info(f"No {kind.lower()} options in this set.")
        return

    rows = [r for r in all_rows if not is_excluded(r.trade_reference, exclusions)]
    excluded_rows = [r for r in all_rows if is_excluded(r.trade_reference, exclusions)]
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.broker or "Unknown", []).append(r)
    excluded_groups: dict[str, list] = {}
    for r in excluded_rows:
        excluded_groups.setdefault(r.broker or "Unknown", []).append(r)

    # trade-ref search (sidebar box); primary owns the shared hit-list + anchor
    query = (st.session_state.get("trade_search") or "").strip().casefold()
    hits = [(broker, str(r.trade_reference))
            for broker, brows in groups.items() for r in brows
            if query and query in str(r.trade_reference or "").casefold()]
    if primary:
        st.session_state["trade_search_hits"] = hits
    else:
        st.session_state["trade_search_put_hits"] = len(hits)
    hit_brokers = {b for b, _ in hits}
    snap_broker = hits[0][0] if hits else None

    gt_mtm = sum(t.mtm_local for t in (section_totals(g) for g in groups.values()))
    gt_shares = sum(t.shares for t in (section_totals(g) for g in groups.values()))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Brokers", len(groups), border=True)
    c2.metric(f"{kind} trades", len(rows), border=True)
    c3.metric("Total MTM (local ccy)", _compact(gt_mtm), border=True, help=f"{gt_mtm:,.2f}")
    c4.metric("Total shares", _compact(gt_shares), border=True, help=f"{gt_shares:,.2f}")

    review = _review_table(rows)
    if review:
        st.warning(f"{len(review)} trade(s) excluded from the Shares subtotal — "
                   "their statement quantity is a notional, not a share count "
                   "(needs trade-terms/traders-DB conversion). MTM is unaffected.")
        with st.expander(f"Shares needing conversion ({len(review)})"):
            st.dataframe(review, width="stretch", hide_index=True)
    if excluded_rows:
        st.info(f"{len(excluded_rows)} trade(s) manually excluded from the sheet "
                "and totals (see each broker's *Excluded* panel to review or restore).")

    out_path = Path(tempfile.gettempdir()) / f"{kind.lower()}_options_{as_of}.xlsx"
    write_call_options_sectioned_xlsx(rows, out_path, as_of=as_of, sheet_label=sheet_label)
    st.download_button(f"Download {sheet_label} — {len(rows)} trades (.xlsx)",
                       out_path.read_bytes(),
                       file_name=f"{kind.lower()}_options_{as_of}.xlsx",
                       mime=XLSX_MIME, type="primary", width="stretch")

    st.divider()
    section_brokers = list(groups) + [b for b in excluded_groups if b not in groups]
    for broker in section_brokers:
        broker_rows = groups.get(broker, [])
        broker_excluded = excluded_groups.get(broker, [])
        t = section_totals(broker_rows)
        is_hit = broker in hit_brokers
        if primary and broker == snap_broker:
            st.markdown('<div id="trade-hit-anchor"></div>', unsafe_allow_html=True)
        display_rows = broker_rows
        if query and is_hit:
            display_rows = sorted(
                broker_rows,
                key=lambda r: query not in str(r.trade_reference or "").casefold())
        warns = _warnings_for_rows(display_rows)
        n_flag = sum(1 for w in warns if w)
        with st.container(border=True):
            badge = f"  :orange-badge[⚠ {n_flag} flagged]" if n_flag else ""
            excl_badge = (f"  :gray-badge[{len(broker_excluded)} excluded]"
                          if broker_excluded else "")
            st.markdown(f"#### {broker}  :blue-badge[{len(broker_rows)} trades]"
                        f"{badge}{excl_badge}")
            search_mask = None
            if is_hit:
                search_mask = pd.Series(
                    [query in str(r.trade_reference or "").casefold()
                     for r in display_rows])
                st.caption(f"{int(search_mask.sum())} search match(es) floated to "
                           "the top and highlighted.")
            if display_rows:
                _flagged_dataframe(_rows_table(display_rows), warns,
                                   column_config=_ROWS_COLUMNS, height=260,
                                   search_mask=search_mask)
                st.markdown(f"**Subtotal (F7)** — MTM (Ccy): **{t.mtm_local:,.2f}** · "
                            f"Shares: **{t.shares:,.2f}**")
                st.caption("F7: shares are positive-only (short legs excluded); "
                           "MTM is a plain sum.")
            else:
                st.caption("All trades for this broker are currently excluded.")
            _anomaly_panel(broker, [r.trade_reference for r in display_rows], warns)
            _valuation_flag_panel(broker, display_rows)
            _exclusions_panel(broker, broker_rows, broker_excluded, scope=kind)

    with st.container(border=True):
        st.markdown("#### Grand total (F8)")
        g1, g2, g3 = st.columns(3)
        g1.metric("MTM (Ccy)", f"{gt_mtm:,.2f}")
        g2.metric("Shares", f"{gt_shares:,.2f}")
        g3.metric(f"{kind} trades", len(rows))


single_tab, month_tab = st.tabs(["Check one statement", "Month-end close"])

# ----------------------------------------------------------------------------
with single_tab:
    st.caption("Check how one statement parses before including it in the month-end run.")
    uploaded = st.file_uploader(
        "Drag a broker statement here — or click to browse",
        type=["xls", "xlsx"], key=f"single_{st.session_state.get('_upload_gen', 0)}",
        help="Drag an .xls/.xlsx straight from your file explorer onto the box.",
    )
    if uploaded is None:
        st.info("**Waiting for a file…**  Known formats (see sidebar) are identified "
                "and parsed instantly; anything new is flagged for onboarding. "
                "If a known broker changed its layout, that's caught here too.")
    else:
        with st.spinner(f"Parsing {uploaded.name} …"):
            path = _save_temp(uploaded)
            try:
                matches = identify(path)
            except Exception as e:  # noqa: BLE001 — unreadable/corrupt/encrypted files
                st.error(f"**Could not read** {uploaded.name}: {e}")
                _halt()

        if len(matches) == 0:
            st.warning(f"**Unrecognized format** — {uploaded.name}. Drop it in the "
                       "**Month-end close** tab to draft a parser for it and "
                       "approve it.")
        elif len(matches) > 1:
            st.warning(f"**Ambiguous** — matched {', '.join(m.broker.value for m in matches)}.")
        else:
            spec = matches[0]
            broker = spec.broker.value
            drift = check_spec_drift(spec, path)
            reonb_key = f"reonb_single::{uploaded.name}"

            if not drift.ok:
                # Hard drift: a REQUIRED column is gone, so the saved spec can no
                # longer parse this file. Flag it clearly (not a generic error)
                # and offer to re-onboard.
                st.error(f"**{broker}'s statement format changed.**  The saved parser "
                         f"can no longer read it — {drift.summary()}")
                _drift_detail(spec, path, uploaded.name)
                if st.button(f"Re-onboard {broker} (propose an updated parser)",
                             key=f"btn::{reonb_key}", type="primary"):
                    st.session_state[reonb_key] = True
                if st.session_state.get(reonb_key):
                    _render_spec_proposal(uploaded.name, path, reonboard=True)
            else:
                marks = run_spec(spec, path)
                if drift.missing_optional:
                    st.warning(f"**{broker}'s format may have changed.**  Parsed OK, but "
                               f"optional column(s) no longer found: "
                               f"**{', '.join(drift.missing_optional)}** — those values will "
                               "be blank until the parser is updated.")
                    _drift_detail(spec, path, uploaded.name)
                else:
                    st.success(f"Identified as **{broker}** — format verified against the "
                               "saved spec.")
                warns = _warnings_for_marks(marks)
                n_flag = sum(1 for w in warns if w)
                c1, c2, c3 = st.columns([1, 1, 2])
                c1.metric("Broker", broker, border=True)
                c2.metric("Trades", len(marks), border=True)
                c3.metric("Flagged rows", n_flag, border=True,
                          help="Rows whose values look unusual vs their peers.")
                _flagged_dataframe(_marks_table(marks), warns,
                                   column_config=_MARKS_COLUMNS, height=320)
                _anomaly_panel(broker, [m.trade_ref for m in marks], warns)

                if drift.missing_optional:
                    if st.button(f"Re-onboard {broker} to capture the new layout",
                                 key=f"btn::{reonb_key}"):
                        st.session_state[reonb_key] = True
                    if st.session_state.get(reonb_key):
                        _render_spec_proposal(uploaded.name, path, reonboard=True)

# ----------------------------------------------------------------------------
with month_tab:
    st.caption("**1.** Drop ALL broker statements  ·  **2.** Confirm the month-end date  ·  "
               "**3.** Review each broker section  ·  **4.** Download the ALIC sheets.")

    # The uploader comes BEFORE the date so the date can be read from the files
    # rather than typed. This used to default to a hard-coded 2026-03-31, which
    # meant dropping in August statements produced a sheet stamped March whose
    # maturity screen compared every expiry against the wrong day and found
    # nothing matured. The statements all state their own valuation date.
    extra = st.file_uploader(
        "Drag broker statements here — or click to browse (multiple at once)",
        type=["xls", "xlsx"], accept_multiple_files=True,
        key=f"month_{st.session_state.get('_upload_gen', 0)}",
        help="Drag one or more .xls/.xlsx files straight from your file explorer.",
    )

    sources = [("upload", _save_temp(f), f.name) for f in (extra or [])]

    stated_dates = _stated_dates([p for _, p, *_ in sources])
    detected = Counter(stated_dates.values()).most_common(1)[0][0] if stated_dates else None

    # Streamlit keeps widget state by key, so once "as_of_month" exists a new
    # value= is IGNORED -- dropping August files would leave the pre-upload date
    # showing, which is the exact bug being fixed. Write the detected date into
    # session state (before the widget is built) whenever detection changes, so
    # a new set of files re-dates the field. A later manual edit still sticks,
    # because that only changes the widget, not _detected_as_of.
    if detected and st.session_state.get("_detected_as_of") != detected:
        st.session_state["_detected_as_of"] = detected
        st.session_state["as_of_month"] = date.fromisoformat(detected)

    date_col, note_col = st.columns([1, 3])
    as_of = date_col.date_input(
        "Month-end date",
        value=st.session_state.get("as_of_month") or _prev_month_end(),
        key="as_of_month",
        help="Taken from the statements' own valuation dates when they state one.",
    ).isoformat()

    if detected:
        with note_col:
            if as_of == detected:
                st.caption(f"Dated from the statements: **{detected}**"
                           f" ({len(stated_dates)} of {len(sources)} files state a date)")
            else:
                st.error(f"**As-of mismatch** — you have **{as_of}** selected but the "
                         f"statements say **{detected}**. A wrong as-of mis-stamps the "
                         f"sheet and makes the matured-trade check compare expiries "
                         f"against the wrong day.")
        odd = {b: d for b, d in stated_dates.items() if d != detected}
        if odd:
            st.info("Not every statement agrees on the date (this can be legitimate — a "
                    "broker may close a day early): "
                    + ", ".join(f"**{b}** {d}" for b, d in sorted(odd.items())))

    all_marks, loaded, errors, dupes = [], [], [], []
    onboardables: list[tuple[str, Path]] = []
    # Known brokers whose LAYOUT drifted vs the saved spec. Hard = a required
    # column vanished (can't parse, excluded); soft = an optional column moved
    # (still parses, flagged). Each entry: (spec, display, path).
    drifted_hard: list[tuple[object, str, Path]] = []
    drifted_soft: list[tuple[object, str, Path]] = []
    seen_brokers: set[str] = set()
    with st.spinner(f"Parsing {len(sources)} files …"):
        for src in sources:
            path = src[1]
            display = src[2] if len(src) > 2 else path.name
            try:
                matches = identify(path)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{display} (error: {e})")
                continue
            if len(matches) == 0:
                onboardables.append((display, path))
                continue
            if len(matches) > 1:
                errors.append(f"{display} (ambiguous: "
                              f"{', '.join(m.broker.value for m in matches)})")
                continue
            spec = matches[0]
            broker = spec.broker.value
            drift = check_spec_drift(spec, path)
            if not drift.ok:
                drifted_hard.append((spec, display, path))
                continue
            if broker in seen_brokers:
                dupes.append(f"{display} ({broker})")
                continue
            try:
                marks = run_spec(spec, path)
            except Exception as e:  # noqa: BLE001 — defensive; drift.ok implies parseable
                errors.append(f"{display} (error: {e})")
                continue
            seen_brokers.add(broker)
            loaded.append((broker, len(marks), display))
            all_marks.extend(marks)
            if drift.missing_optional:
                drifted_soft.append((spec, display, path))

    if loaded:
        st.success(f"Loaded {len(loaded)} brokers — {len(all_marks)} trades.")
        with st.expander(f"Files loaded ({len(loaded)})"):
            st.dataframe([{"Broker": broker, "Trades": n, "File": name}
                          for broker, n, name in loaded],
                         width="stretch", hide_index=True)
            if dupes:
                st.warning("Skipped (broker already loaded): " + ", ".join(dupes))
            if errors:
                st.error("Skipped (unreadable): " + ", ".join(errors))

    # Format-change alerts. Detection is cheap and runs for every known file;
    # the re-onboard drafting (a model call) fires only when the user clicks and
    # renders inline right here, so tokens are never spent by surprise.
    if drifted_hard or drifted_soft:
        st.subheader("Format changes detected")
        st.caption("A known broker's statement layout changed vs the saved parser. "
                   "Review what moved, then re-onboard to update the parser if it's a "
                   "real change.")
        for spec, display, path in drifted_hard:
            broker = spec.broker.value
            rep = check_spec_drift(spec, path)
            st.error(f"**{broker}** — `{display}` no longer parses with the saved spec "
                     f"({rep.summary()}). **Excluded** from the totals below until re-onboarded.")
            _drift_detail(spec, path, display)
            key = f"reonb_month::{display}"
            if st.button(f"Re-onboard {broker} (propose an updated parser)",
                         key=f"btn::{key}", type="primary"):
                st.session_state[key] = True
            if st.session_state.get(key):
                _render_spec_proposal(display, path, reonboard=True)
        for spec, display, path in drifted_soft:
            broker = spec.broker.value
            rep = check_spec_drift(spec, path)
            st.warning(f"**{broker}** — `{display}` parsed and is included below, but optional "
                       f"column(s) no longer found: **{', '.join(rep.missing_optional)}** "
                       "(those values are blank in the output).")
            _drift_detail(spec, path, display)
            key = f"reonb_month::{display}"
            if st.button(f"Re-onboard {broker} to capture the new layout",
                         key=f"btn::{key}"):
                st.session_state[key] = True
            if st.session_state.get(key):
                _render_spec_proposal(display, path, reonboard=True)

    # Unknown formats get their own flagged section, filled in AFTER the known
    # brokers render below — the page never blocks on the model call.
    onboard_area = None
    if onboardables:
        onboard_area = st.container()
        with onboard_area:
            names = ", ".join(f"`{d}`" for d, _ in onboardables)
            st.warning(f"**Unknown format** — {names}. Drafting a parser for your review…")

    if not all_marks and not onboardables and not drifted_hard and not drifted_soft:
        st.info("**Drop broker statements above to build the combined sheet.**  "
                "You can drop every file at once; each broker gets its own section "
                "with an F7 subtotal, plus the F8 grand total and download buttons.")
        _halt()

    if all_marks:
        # Screen every entry: drop exact repeats, flag matured (kept for review).
        screen = screen_marks(all_marks, as_of)
        if screen.repeats or screen.matured or screen.unknown_maturity:
            cols = st.columns(3)
            cols[0].metric("Duplicates removed", len(screen.repeats), border=True)
            cols[1].metric("Matured, kept for review", len(screen.matured), border=True)
            cols[2].metric("Maturity not stated", len(screen.unknown_maturity), border=True)
            if screen.matured:
                with st.expander(f"Matured entries — {len(screen.matured)} (past maturity as of {as_of})"):
                    st.dataframe([{"Broker": f.broker, "Trade Ref": f.trade_ref, "Detail": f.detail}
                                  for f in screen.matured], width="stretch", hide_index=True)
            if screen.repeats:
                with st.expander(f"Repeat entries dropped — {len(screen.repeats)}"):
                    st.dataframe([{"Broker": f.broker, "Trade Ref": f.trade_ref}
                                  for f in screen.repeats], width="stretch", hide_index=True)
        marks_for_output = screen.kept

        # Calls and puts each get the FULL sectioned treatment in their own
        # sub-tab (sections, F7 subtotals, flags, exclusions, F8 total, .xlsx),
        # driven by the same renderer. Manual exclusions are global by trade
        # ref, so the same exclusion list applies to whichever sheet the ref
        # lands in.
        call_marks, put_marks = split_marks_by_option_type(marks_for_output)
        exclusions = load_exclusions()
        calls_tab, puts_tab = st.tabs(
            [f"Call options ({len(call_marks)})", f"Put options ({len(put_marks)})"])
        with calls_tab:
            _render_option_sheet("Call", call_marks, as_of=as_of,
                                 exclusions=exclusions, primary=True)
        with puts_tab:
            _render_option_sheet("Put", put_marks, as_of=as_of,
                                 exclusions=exclusions, primary=False)

    # Onboard unknown formats LAST, so everything above rendered first; the
    # results appear in the flagged section created near the top of the tab.
    if onboardables:
        with onboard_area:
            for display, path in onboardables:
                _render_spec_proposal(display, path)

# rendered last so it reflects any tokens spent during this run
_render_sidebar()
