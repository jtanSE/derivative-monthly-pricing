"""Drag-and-drop interface for parsing broker statements.

Run:  uv run --group ui streamlit run ui/app.py

Two modes:
- Single statement: drop one file, see how it parses.
- Month-end combined: drop ALL broker files, see the ALIC-shaped sheet
  (per-broker sections, F7 subtotals, F8 grand total) in one place + download.

The actual files are parsed via the router -- never screenshots.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import streamlit as st

from derivative_pricing.adaptive.router import route
from derivative_pricing.output import build_rows_from_marks, write_call_options_sectioned_xlsx
from derivative_pricing.recon import section_totals

REPO = Path(__file__).resolve().parents[1]
# Where to look for already-present statements. Defaults to the project folder
# for local dev; in a hosted container set STATEMENTS_DIR to a mounted volume
# (or leave it empty and rely on the uploader). Real broker data is never baked
# into the image.
STATEMENTS_DIR = Path(os.environ.get("STATEMENTS_DIR", str(REPO)))

st.set_page_config(page_title="Derivative MTM Parser", page_icon="📄", layout="wide")
st.title("📄 Broker Statement Parser")
st.caption("Known formats parse instantly; new ones are flagged for onboarding. "
           "The actual files are parsed — no screenshots.")


def _save_temp(uploaded) -> Path:
    suffix = Path(uploaded.name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded.getbuffer())
    return Path(tmp.name)


def _marks_table(marks):
    return [
        {
            "Trade Ref": m.trade_ref,
            "MTM (local)": round(m.mtm_local, 2),
            "Ccy": m.currency,
            "Underlying": m.extra.get("underlying"),
            "Strike": m.extra.get("strike"),
            "Quantity": m.extra.get("quantity"),
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
            "Notional (Ccy)": None if r.notional_local is None else round(r.notional_local, 2),
            "Notional (USD)": None if r.notional_usd is None else round(r.notional_usd, 2),
            "MTM (Ccy)": None if r.mtm_local is None else round(r.mtm_local, 2),
            "MTM (USD)": None if r.mtm_usd is None else round(r.mtm_usd, 2),
            "Shares": r.shares,
            "Price (Ccy)": None if r.price_local is None else round(r.price_local, 4),
        }
        for r in rows
    ]


single_tab, month_tab = st.tabs(["Single statement", "Month-end combined (ALIC sheet)"])

# ----------------------------------------------------------------------------
with single_tab:
    uploaded = st.file_uploader("Drop a broker statement", type=["xls", "xlsx"],
                                key="single")
    if uploaded is None:
        st.info("Waiting for a file…  (BNP, BAML, Goldman Sachs and HSBC are known.)")
    else:
        with st.spinner(f"Parsing {uploaded.name} …"):
            result = route(_save_temp(uploaded))
        if result.status == "unknown":
            st.warning(f"**Unrecognized format** — {uploaded.name}. Would be handed to "
                       "the onboarding agent to draft a new spec.")
        elif result.status == "ambiguous":
            st.warning(f"**Ambiguous** — matched {', '.join(result.candidates)}.")
        else:
            st.success(f"✅ Identified as **{result.broker}** — {len(result.marks)} trades.")
            st.dataframe(_marks_table(result.marks), use_container_width=True, height=320)

# ----------------------------------------------------------------------------
with month_tab:
    as_of = st.text_input("Month-end date", value="2026-03-31")
    st.markdown("Combined ALIC sheet built from any statements found in the configured "
                "folder, plus anything you drop below.")

    extra = st.file_uploader("Add broker statements", type=["xls", "xlsx"],
                             accept_multiple_files=True, key="month")

    # Sources: files already present in STATEMENTS_DIR + any uploaded now.
    repo_files = sorted(p for p in STATEMENTS_DIR.glob("*.xls") if " - Copy" not in p.name)
    repo_files += sorted(STATEMENTS_DIR.glob("*.xlsx"))
    sources = [("repo", p) for p in repo_files] + \
              [("upload", _save_temp(f), f.name) for f in (extra or [])]

    all_marks, loaded, unknown, dupes = [], [], [], []
    seen_brokers: set[str] = set()
    with st.spinner(f"Parsing {len(sources)} files …"):
        for src in sources:
            path = src[1]
            display = src[2] if len(src) > 2 else path.name
            try:
                r = route(path)
            except Exception as e:  # noqa: BLE001
                unknown.append(f"{display} (error: {e})")
                continue
            if r.status != "known":
                if r.status == "unknown":
                    unknown.append(display)
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
            if unknown:
                st.write("Skipped (unrecognized / not a statement): " + ", ".join(unknown))
    if not all_marks:
        st.info("No recognized broker statements found in the project folder.")
        st.stop()

    rows = build_rows_from_marks(all_marks, fx_rates={"USD": 1.0})

    # group rows by broker, preserving order
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.broker or "Unknown", []).append(r)

    # grand-total metrics
    gt_notl = sum(t.notional_local for t in (section_totals(g) for g in groups.values()))
    gt_mtm = sum(t.mtm_local for t in (section_totals(g) for g in groups.values()))
    gt_shares = sum(t.shares for t in (section_totals(g) for g in groups.values()))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Brokers", len(groups))
    c2.metric("Trades", len(rows))
    c3.metric("Grand Notional (Ccy)", f"{gt_notl:,.0f}")
    c4.metric("Grand MTM (Ccy)", f"{gt_mtm:,.0f}")

    # download the real ALIC-shaped xlsx
    out_path = Path(tempfile.gettempdir()) / f"call_options_{as_of}.xlsx"
    write_call_options_sectioned_xlsx(rows, out_path, as_of=as_of)
    st.download_button("⬇ Download ALIC-shaped .xlsx", out_path.read_bytes(),
                       file_name=f"call_options_{as_of}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.divider()
    # render each broker section with its F7 subtotal
    for broker, broker_rows in groups.items():
        t = section_totals(broker_rows)
        st.subheader(broker)
        st.dataframe(_rows_table(broker_rows), use_container_width=True, height=260)
        st.markdown(
            f"**{broker} subtotal** — Notional (Ccy): `{t.notional_local:,.2f}` · "
            f"MTM (Ccy): `{t.mtm_local:,.2f}` · Shares: `{t.shares:,.2f}`  \n"
            f"<span style='color:gray'>F7: notional & shares are positive-only "
            f"(short legs excluded); MTM is a plain sum.</span>",
            unsafe_allow_html=True,
        )
        st.divider()

    st.markdown(f"## GRAND TOTAL  \n"
                f"Notional (Ccy): `{gt_notl:,.2f}` · MTM (Ccy): `{gt_mtm:,.2f}` · "
                f"Shares: `{gt_shares:,.2f}`")
