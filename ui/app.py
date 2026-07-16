"""Drag-and-drop interface for parsing broker statements.

Run:  uv run --group ui streamlit run ui/app.py

Two modes:
- Single statement: drop one file, see how it parses.
- Month-end combined: drop ALL broker files, see the ALIC-shaped sheet
  (per-broker sections, F7 subtotals, F8 grand total) in one place + download.

The actual files are parsed via the router -- never screenshots.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import streamlit as st

from derivative_pricing.adaptive.agent import (
    load_reference_specs,
    onboard_file,
    save_spec,
)
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.adaptive.router import route
from derivative_pricing.ingest import screen_marks
from derivative_pricing.output import (
    build_rows_from_marks,
    split_marks_by_option_type,
    write_call_options_sectioned_xlsx,
)
from derivative_pricing.recon import section_totals

st.set_page_config(page_title="Derivative MTM Parser", page_icon="📄", layout="wide")
st.title("📄 Broker Statement Parser")
st.caption("Known formats parse instantly; new ones are flagged for onboarding. "
           "The actual files are parsed — no screenshots.")


def _save_temp(uploaded) -> Path:
    suffix = Path(uploaded.name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded.getbuffer())
    return Path(tmp.name)


def _unit_price(m):
    """F5 per-unit price = MTM / signed shares (short legs -> negative shares).

    MTM here is as-reported by the broker (no sign convention applied yet);
    None when the statement carries no quantity (e.g. Nomura)."""
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
            "Shares": r.shares,
            "Price (Ccy)": None if r.price_local is None else round(r.price_local, 4),
        }
        for r in rows
    ]


def _render_onboarding(display: str, path: Path) -> None:
    """Unknown statement -> onboarding agent -> preview -> human approve/save.

    The drafted spec is cached in session_state so reruns (e.g. button clicks)
    never re-invoke the model; approving writes the spec to disk, after which
    the file parses as a known broker like any other."""
    cache_key = f"onboard::{display}"
    if cache_key not in st.session_state:
        with st.spinner(f"🤖 Onboarding agent drafting a parser spec for `{display}` … "
                        "(known brokers are already loaded below)"):
            st.session_state[cache_key] = onboard_file(path)
    res = st.session_state[cache_key]

    if res.status == "already_known":
        st.info(f"`{display}` is already covered by the **{res.broker}** spec — re-drop to parse.")
        return
    if res.status in ("needs_key", "disabled", "rejected"):
        st.error(f"`{display}` — {res.message}")
        if res.validation is not None and res.validation.issues:
            st.write("Validation issues: " + "; ".join(res.validation.issues))
        if st.button("Retry onboarding", key=f"retry::{display}"):
            del st.session_state[cache_key]
            st.rerun()
        return

    # status == "proposed": drafted + validated. Preview in exactly the same
    # shape as a known broker section, clearly marked as not yet approved.
    v = res.validation
    st.success(f"🆕 Spec drafted for **{res.broker}** and validated — "
               f"{v.rows_parsed} rows · {v.pct_with_quantity:.0%} with quantity · "
               f"{v.pct_with_strike:.0%} with strike.")
    marks = run_spec(res.spec, path)
    call_marks, put_marks = split_marks_by_option_type(marks)
    preview_rows = build_rows_from_marks(call_marks, fx_rates={"USD": 1.0})
    st.subheader(f"{res.broker} — {len(preview_rows)} trades — PROPOSED (not in totals until approved)")
    st.dataframe(_rows_table(preview_rows), use_container_width=True, height=240)
    if preview_rows:
        t = section_totals(preview_rows)
        st.markdown(f"**{res.broker} subtotal (preview)** — "
                    f"MTM (Ccy): `{t.mtm_local:,.2f}` · Shares: `{t.shares:,.2f}`")
    if put_marks:
        st.caption(f"➕ {len(put_marks)} put option(s) will route to the Put Options "
                   "sheet once approved.")
    with st.expander("Drafted parser spec (JSON)"):
        st.code(res.spec.model_dump_json(indent=2), language="json")
    if st.button(f"✅ Approve & save the {res.broker} parser", key=f"approve::{display}"):
        try:
            saved = save_spec(res.spec)
        except FileExistsError as e:
            st.error(str(e))
            return
        del st.session_state[cache_key]
        st.success(f"Saved `{saved.name}` — {res.broker} is now a known format.")
        st.rerun()


single_tab, month_tab = st.tabs(["Single statement", "Month-end combined (ALIC sheet)"])

# ----------------------------------------------------------------------------
with single_tab:
    uploaded = st.file_uploader(
        "Drag a broker statement here — or click to browse",
        type=["xls", "xlsx"], key="single",
        help="Drag an .xls/.xlsx straight from your file explorer onto the box.",
    )
    if uploaded is None:
        known = ", ".join(sorted(s.broker.value for s in load_reference_specs()))
        st.info(f"Waiting for a file…  (Known formats: {known}.)")
    else:
        with st.spinner(f"Parsing {uploaded.name} …"):
            try:
                result = route(_save_temp(uploaded))
            except Exception as e:  # noqa: BLE001 — unreadable/corrupt/encrypted files
                st.error(f"**Could not read** {uploaded.name}: {e}")
                st.stop()
        if result.status == "unknown":
            st.warning(f"**Unrecognized format** — {uploaded.name}. Drop it in the "
                       "**Month-end combined** tab to run the onboarding agent "
                       "and approve a new parser.")
        elif result.status == "ambiguous":
            st.warning(f"**Ambiguous** — matched {', '.join(result.candidates)}.")
        else:
            st.success(f"✅ Identified as **{result.broker}** — {len(result.marks)} trades.")
            st.dataframe(_marks_table(result.marks), use_container_width=True, height=320)

# ----------------------------------------------------------------------------
with month_tab:
    as_of = st.text_input("Month-end date", value="2026-03-31")
    st.markdown("Combined ALIC sheet built from the statements you drop below.")

    extra = st.file_uploader(
        "Drag broker statements here — or click to browse (multiple at once)",
        type=["xls", "xlsx"], accept_multiple_files=True, key="month",
        help="Drag one or more .xls/.xlsx files straight from your file explorer.",
    )

    sources = [("upload", _save_temp(f), f.name) for f in (extra or [])]

    all_marks, loaded, errors, dupes = [], [], [], []
    onboardables: list[tuple[str, Path]] = []
    seen_brokers: set[str] = set()
    with st.spinner(f"Parsing {len(sources)} files …"):
        for src in sources:
            path = src[1]
            display = src[2] if len(src) > 2 else path.name
            try:
                r = route(path)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{display} (error: {e})")
                continue
            if r.status != "known":
                if r.status == "unknown":
                    onboardables.append((display, path))
                continue
            if r.broker in seen_brokers:
                dupes.append(f"{display} ({r.broker})")
                continue
            seen_brokers.add(r.broker)
            loaded.append((r.broker, len(r.marks), display))
            all_marks.extend(r.marks)

    if loaded:
        st.success(f"Loaded {len(loaded)} brokers — {len(all_marks)} trades.")
        with st.expander("Files loaded"):
            for broker, n, name in loaded:
                st.write(f"• **{broker}** — {n} trades  ·  `{name}`")
            if dupes:
                st.write("Skipped (broker already loaded): " + ", ".join(dupes))
            if errors:
                st.write("Skipped (unreadable): " + ", ".join(errors))

    # Unknown formats get their own flagged section, filled in AFTER the known
    # brokers render below — the page never blocks on the model call.
    onboard_area = None
    if onboardables:
        onboard_area = st.container()
        with onboard_area:
            names = ", ".join(f"`{d}`" for d, _ in onboardables)
            st.warning(f"⚠️ **Unknown format** — {names} → sending to the onboarding agent…")

    if not all_marks and not onboardables:
        st.info("Drop broker statements above to build the combined sheet.")
        st.stop()

    if all_marks:
        # Screen every entry: drop exact repeats, flag matured (kept for review).
        screen = screen_marks(all_marks, as_of)
        if screen.repeats or screen.matured or screen.unknown_maturity:
            cols = st.columns(3)
            cols[0].metric("Repeat entries (dropped)", len(screen.repeats))
            cols[1].metric("Matured (flagged, kept)", len(screen.matured))
            cols[2].metric("Unknown maturity", len(screen.unknown_maturity))
            if screen.matured:
                with st.expander(f"Matured entries — {len(screen.matured)} (past maturity as of {as_of})"):
                    st.dataframe([{"Broker": f.broker, "Trade Ref": f.trade_ref, "Detail": f.detail}
                                  for f in screen.matured], use_container_width=True)
            if screen.repeats:
                with st.expander(f"Repeat entries dropped — {len(screen.repeats)}"):
                    st.dataframe([{"Broker": f.broker, "Trade Ref": f.trade_ref}
                                  for f in screen.repeats], use_container_width=True)
        marks_for_output = screen.kept

        # Calls and puts go to separate ALIC sheets (puts must not inflate the
        # calls totals). The main view below is the calls sheet; puts get their
        # own download button and count.
        call_marks, put_marks = split_marks_by_option_type(marks_for_output)
        rows = build_rows_from_marks(call_marks, fx_rates={"USD": 1.0})
        put_rows = build_rows_from_marks(put_marks, fx_rates={"USD": 1.0}) if put_marks else []

        # group rows by broker, preserving order
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r.broker or "Unknown", []).append(r)

        # grand-total metrics
        gt_mtm = sum(t.mtm_local for t in (section_totals(g) for g in groups.values()))
        gt_shares = sum(t.shares for t in (section_totals(g) for g in groups.values()))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Brokers", len(groups))
        c2.metric("Call trades", len(rows))
        c3.metric("Grand MTM (Ccy)", f"{gt_mtm:,.0f}")
        c4.metric("Grand Shares", f"{gt_shares:,.0f}")
        if put_rows:
            st.caption(f"➕ {len(put_rows)} put option(s) split into a separate Put "
                       "Options sheet (kept out of the calls totals above).")

        # download the ALIC-shaped calls sheet + a matching puts sheet
        dl1, dl2 = st.columns(2)
        call_path = Path(tempfile.gettempdir()) / f"call_options_{as_of}.xlsx"
        write_call_options_sectioned_xlsx(rows, call_path, as_of=as_of,
                                          sheet_label="Call Options")
        dl1.download_button(f"⬇ Call Options .xlsx ({len(rows)} trades)",
                            call_path.read_bytes(),
                            file_name=f"call_options_{as_of}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if put_rows:
            put_path = Path(tempfile.gettempdir()) / f"put_options_{as_of}.xlsx"
            write_call_options_sectioned_xlsx(put_rows, put_path, as_of=as_of,
                                              sheet_label="Put Options")
            dl2.download_button(f"⬇ Put Options .xlsx ({len(put_rows)} trades)",
                                put_path.read_bytes(),
                                file_name=f"put_options_{as_of}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            dl2.info("No put options in this set.")

        st.divider()
        # render each broker section with its F7 subtotal
        for broker, broker_rows in groups.items():
            t = section_totals(broker_rows)
            st.subheader(f"{broker} — {len(broker_rows)} trades")
            st.dataframe(_rows_table(broker_rows), use_container_width=True, height=260)
            st.markdown(
                f"**{broker} subtotal** — Trades: `{len(broker_rows)}` · "
                f"MTM (Ccy): `{t.mtm_local:,.2f}` · Shares: `{t.shares:,.2f}`  \n"
                f"<span style='color:gray'>F7: shares are positive-only "
                f"(short legs excluded); MTM is a plain sum.</span>",
                unsafe_allow_html=True,
            )
            st.divider()

        st.markdown(f"## GRAND TOTAL  \n"
                    f"MTM (Ccy): `{gt_mtm:,.2f}` · "
                    f"Shares: `{gt_shares:,.2f}`")

    # Onboard unknown formats LAST, so everything above rendered first; the
    # results appear in the flagged section created near the top of the tab.
    if onboardables:
        with onboard_area:
            for display, path in onboardables:
                _render_onboarding(display, path)
