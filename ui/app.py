"""Drag-and-drop interface for parsing broker statements.

Run:  uv run --group ui streamlit run ui/app.py

Drop a broker statement (.xls/.xlsx). The app routes it: a known broker is
parsed deterministically and the results shown; an unknown format is flagged
for onboarding. The actual file is parsed -- never a screenshot.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# make the src package importable when run via `streamlit run`
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import streamlit as st

from derivative_pricing.adaptive.router import route
from derivative_pricing.output import build_rows_from_marks

st.set_page_config(page_title="Derivative MTM Parser", page_icon="📄", layout="wide")

st.title("📄 Broker Statement Parser")
st.caption(
    "Drag in a month-end broker statement. Known formats are parsed instantly; "
    "new ones are flagged for onboarding. The actual file is parsed — no screenshots."
)

uploaded = st.file_uploader(
    "Drop a broker statement here", type=["xls", "xlsx"], accept_multiple_files=False
)

if uploaded is None:
    st.info("Waiting for a file…  (BNP, BAML, Goldman Sachs and HSBC are currently known.)")
    st.stop()

# Persist the upload to a temp file, preserving extension (router dispatches on it).
suffix = Path(uploaded.name).suffix
with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
    tmp.write(uploaded.getbuffer())
    tmp_path = Path(tmp.name)

with st.spinner(f"Parsing {uploaded.name} …"):
    try:
        result = route(tmp_path)
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not read the file: {e}")
        st.stop()

if result.status == "unknown":
    st.warning(
        f"**Unrecognized format.** No existing spec matches **{uploaded.name}**.\n\n"
        "This would be handed to the onboarding agent to draft a new parser spec "
        "(then validated and saved after review)."
    )
    st.stop()

if result.status == "ambiguous":
    st.warning(
        f"**Ambiguous** — this file matched more than one known spec: "
        f"{', '.join(result.candidates)}. A human needs to disambiguate."
    )
    st.stop()

# status == "known"
marks = result.marks
st.success(f"✅ Identified as **{result.broker}** — parsed **{len(marks)} trades**.")

total_mtm = sum(m.mtm_local for m in marks)
distinct = len({m.trade_ref for m in marks})
c1, c2, c3 = st.columns(3)
c1.metric("Broker", result.broker)
c2.metric("Trades parsed", len(marks))
c3.metric("Total MTM (local)", f"{total_mtm:,.2f}")

st.subheader("Parsed marks")
st.dataframe(
    [
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
    ],
    use_container_width=True,
    height=360,
)

st.subheader("ALIC-style output (computed)")
rows = build_rows_from_marks(marks, fx_rates={"USD": 1.0})
st.dataframe(
    [
        {
            "Trade Reference": r.trade_reference,
            "Index": r.index,
            "Strike": r.strike_rate,
            "Maturity": r.maturity_date,
            "Currency": r.currency,
            "Shares": r.shares,
            "Notional (Ccy)": None if r.notional_local is None else round(r.notional_local, 2),
            "MTM (Ccy)": None if r.mtm_local is None else round(r.mtm_local, 2),
            "Price (Ccy)": None if r.price_local is None else round(r.price_local, 4),
        }
        for r in rows
    ],
    use_container_width=True,
    height=360,
)

st.caption(f"Source: {uploaded.name} · sheet {marks[0].lineage.sheet!r}" if marks else "")
