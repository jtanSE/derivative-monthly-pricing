# SYSTEMIMPLEMENTATION.md
## Month-End Derivative Pricing Automation Platform — Implementation Plan

> **How to read this document**
> - `[ ]` / `[x]` checkboxes track implementation progress. Check items off as they are completed.
> - Every design decision carries a confidence tag:
>   - **`VERIFIED`** — proven from the operational workbook, formula extraction, or explicit answers from the project owner.
>   - **`INFERRED`** — my reasoned conclusion from evidence; challenge these before relying on them.
>   - **`TBD`** — open question. Do **not** build against these until resolved.
> - Source artifacts: `originalpromptplan.md` (requirements) and `ALIC MTM Prices for Call Options Test - 31 March 2026.xls` (sanitized canonical workbook, March 2026 month-end).

---

## Build Status

Implemented and tested (39 passing tests):

- `schema.py` — canonical models (Trade, BrokerMark, CanonicalRecord, statuses)
- `pricing/engine.py` — F1, F2/F4 (Excel ROUND), F5; validated vs the workbook's ~1,400 rows
- **`adaptive/`** — the parsing system:
  - `spec.py` + `engine.py` — declarative per-broker specs run by one generic engine
  - `reader.py` — format-agnostic `.xls`/`.xlsx` reader
  - `agent.py` — AI proposer (fingerprint → few-shot → spec) + deterministic validation gate
  - `router.py` — recognizes known files (run deterministically) vs unknown (hand off to agent)
  - `specs/{bnp,baml,gs,hsbc}.json` — four validated brokers (2 formats, 3 layouts)
- `parsers/base.py` — `register_parser`/`PARSER_REGISTRY` escape hatch + F3 sign helpers; `xls_compat.py` SUPBOOK-tolerant open; `marks_sheet.py` consolidated-Marks reader (validation)
- `reference/` — index canonicalization + Bloomberg market-data seam
- `workbook/call_options.py` — analyst-sheet reader for the parallel-run gate
- `output/call_options_sheet.py` — ALIC-style `.xlsx`/`.csv` writer
- `matching/engine.py`, `recon/engine.py` — skeletons with verified F7 totals semantics

Blocked on real (sanitized) files: more broker statements, traders DB export (Q1).

> Note: MTM sign convention currently lives in two places — `spec.mtm_sign` (engine)
> and `BROKER_MTM_SIGN`/`apply_sign_convention` (output layer). All current brokers
> are +1 so they agree; consolidate to one source before adding Barclays/Nomura
> specs (which flip) to avoid a double-flip.

---

## 0. System Goal

Replace the manually maintained month-end MTM pricing workbook with a **deterministic, auditable, scalable monthly batch platform** that:

1. Ingests month-end broker statements (10 brokers, heterogeneous Excel formats). `VERIFIED`
2. Uses the traders database Excel export as the trade-population source of truth. `VERIFIED`
3. Accepts analyst-entered Bloomberg month-end index prices and FX rates (manual today; API-ready seam for the future). `VERIFIED`
4. Computes notionals, USD conversions, and per-unit prices exactly as the Lead Operations Analyst's workbook does today. `VERIFIED`
5. Computes a derived MTM for validation, but treats **broker statement MTM as the trusted/primary value**. `VERIFIED` (owner decision)
6. Reconciles derived vs broker MTM with tolerance thresholds and explainable exceptions. `VERIFIED` (requirement) / thresholds `TBD`
7. Produces the **canonical live-trades CSV/table** as the MVP final output. PAM message generation is Phase 2+. `VERIFIED`
8. Ingests both calls and puts; prices/reconciles **calls only** in MVP, flags puts as out-of-scope rows. `VERIFIED` (owner decision)

---

## 1. What We Know About the Current Operational Workbook

### 1.1 Workbook structure `VERIFIED`

File: `ALIC MTM Prices for Call Options Test - 31 March 2026.xls` (6 sheets). Only two sheets matter operationally (owner confirmed):

| Sheet | Role |
|---|---|
| `Call Options` (1,549×26) | **The canonical operational output.** All computation lives here. |
| `Marks` (434×31) | **Consolidated broker MTM feed.** Pure pasted data — zero formulas. |

Other sheets (`Sheet1`–`Sheet4`) are scratch/reference; the system must be able to produce everything without them. `VERIFIED` (owner confirmed)

The real production workbook lives at
`S:\INVEST\invest2\inv2026\Monthly Operations (2026)\Derivatives\032026 Derivatives\Options - Calls\MTM Pricing\Call Options Pricing Data - PAM Message - 31 March 2026.xlsx` (sheet `ALIC`) — discovered from a `CELL("filename")` footer. `VERIFIED`

### 1.2 `Call Options` sheet layout `VERIFIED`

Ten vertically stacked **broker sections**, each: banner row (broker name) → title row → header row → trade rows → subtotal row.

| Broker | Banner row | Approx. data rows | ~Trades |
|---|---|---|---|
| Barclays Custom | 3 | 7–438 | ~432 |
| Morgan Stanley | 442 | 446–449 | ~4 |
| BNP Paribas | 453 | 457–750 | ~294 |
| BAML | 754 | 758–828 | ~71 |
| Goldman Sachs | 832 | 836–1145 | ~310 |
| Wells Fargo | 1149 | 1153–1157 | ~5 |
| Nomura | 1161 | 1165–1462 | ~298 |
| JP Morgan | 1466 | 1470–1499 | ~30 |
| Credit Suisse | 1503 | 1507–1509 | ~3 |
| HSBC | 1513 | 1517–1535 | ~19 |

Columns: `A` Cusip · `B` Index · `C` Trade Reference · `D` Strike Rate · `E` Maturity Date · `F` Currency · `G` Notional (Ccy) · `H` Notional (USD) · `I` MTM (Ccy) · `J` MTM (USD) · `K` Shares · `L` Price (Ccy) · `M` (unheaded: raw broker MTM lookup) · `P–S` portfolio block (118 / 119 / Total / Diff) · `T–U` FX rate table · `V–X` month-end index price table.

### 1.3 Extracted formula logic — the operational calculation rules

These are the rules the system must reproduce exactly:

| # | Rule | Source formula (representative) | Status |
|---|---|---|---|
| F1 | **Notional (Ccy) = Shares × month-end index price**, index price looked up by index name | `G7 =+K7*VLOOKUP(B7,$W$8:$X$888,2,FALSE)` | `VERIFIED` |
| F2 | **Notional (USD) = ROUND(Notional Ccy × FX rate, 2)**, FX rate looked up by currency code | `H7 =ROUND(+G7*VLOOKUP($F7,$T$8:$U$50,2,FALSE),2)` | `VERIFIED` |
| F3 | **MTM (Ccy) = broker MTM from Marks**, with **sign flipped for Barclays AND Nomura** (`=-M`), pass-through for the other 8. Empirically confirmed 100% consistent: Barclays 431/431 flipped, Nomura 297/297 flipped, all others pass-through. These two report MTM from the dealer's perspective. | `I7 =-M7` vs `I457 =M457` | `VERIFIED` |
| F4 | **MTM (USD) = ROUND(MTM Ccy × FX rate, 2)** | `J7 =ROUND(+I7*VLOOKUP($F7,...),2)` | `VERIFIED` |
| F5 | **Price (Ccy) = MTM (Ccy) / Shares** — normalized per-unit price | `L7 =+I7/K7` | `VERIFIED` |
| F6 | **Raw broker MTM (col M)**: per-broker lookup from `Marks` by Trade Reference. Barclays/GS/Nomura/JPM/HSBC use `INDEX/MATCH` on their column pairs; BNP & BAML sections use direct cell references (fragile, must be replaced by keyed lookup) | `M7 =INDEX(Marks!$B:$B,MATCH(C7,Marks!$A:$A,0))`, `M457 =Marks!E96`, `M758 =Marks!Q3` | `VERIFIED` |
| F7 | **Per-broker subtotals**: Notional & Shares subtotals are **positive-only** (`SUMIFS(...,">0")`); MTM subtotals are plain `SUM` | rows 439, 751, 829, 1146, 1463, 1500, 1536, … | `VERIFIED` |
| F8 | **Grand totals** (row 1542): sum of the 10 broker subtotal rows per column | `G1542 =G439+G450+G751+...` | `VERIFIED` |
| F9 | **Portfolio allocation (P/Q "118"/"119")**: `VLOOKUP(<key>&"-118", Sheet3!$F$2:$U$173, 16, FALSE)` — splits each trade across PFI portfolios 118 and 119; `R = P+Q`; `S (Diff)` = source value − Total | columns P–S | `VERIFIED` (mechanics) / key column scrubbed (`#REF!`) in test file, exact key `TBD` |
| F10 | **Final cross-check** (col J): `J1546 = J1544+J1545`; `J1547 = J1542 − J1546` — grand-total variance vs two unidentified source rows | rows 1546–1547 | `VERIFIED` (formula) / meaning of rows 1544–1545 `TBD` |
| F11 | **Multi-leg shares**: some Shares cells are hand-keyed sums, with paired +/− rows for offsetting legs (e.g. `K790 =1446.13+3835.19`, `K791 =-1446.13-3835.19`) | Barclays & BAML sections | `VERIFIED` (existence) / business rule `TBD` |

### 1.4 `Marks` sheet layout `VERIFIED`

Wide table; each broker owns a column pair (Trade Ref + MTM value) with broker-specific header naming:

| Broker | ID column | MTM column | ID header | MTM header |
|---|---|---|---|---|
| Barclays | A | B | `Trade Ref.` | `MTM (Ccy)` |
| BNP Paribas | D | E | `BNPP's ref` | `Mark to Market` |
| Morgan Stanley | G | H | `Cusip` | `MTM` |
| Goldman Sachs | J | K | `GS Ref` | `NPV Local` |
| Wells Fargo | M | N | `WF Trade ID` | `MktValue($)` |
| BAML | P | Q | `ML Ref.` | `Estimated mid-market value` |
| Credit Suisse | T | U | `CS Ref` | `Mid-Price` |
| Nomura | X | Y | `Nomura Trade ID` | `valuationNativeCurrency…` |
| JP Morgan | AA | AB | `Deal ID` | `MTM Amount` |
| HSBC | AD | AE | `Deal ID` | `MTM` |

This is effectively the **normalized output contract for the broker parser layer**: every broker parser must emit at minimum `(broker, trade_ref, mtm_local)` — plus whatever richer fields the statement provides.

### 1.5 Known data-quality issues the system must handle `VERIFIED`

- [ ] Index-name canonicalization: `"S&P DYNAMIC INTRADAY TCA"` (BNP rows) vs `"S&P Dynamic Intraday TCA"` (GS rows) — same index, different case.
- [ ] The Barclays section writes the literal string `"Barclays"` in the Index column instead of the actual custom index name (BXIIGTB5, MQIDBA10, …). Real index ticker only appears in the W/X lookup table.
- [x] Barclays and Nomura MTM sign conventions are opposite to the other brokers (F3) — handled in `parsers/base.py`.
- [ ] Direct cell references (`=Marks!E96`) instead of keyed lookups for BNP/BAML — row-order dependent and fragile; the system must always join on trade ref keys.
- [ ] VLOOKUP table ranges are inconsistent across sections (`$X$888` vs `$X$611` …) — system uses one unified price table.
- [ ] Trade reference formats vary by broker: `SWD…`/`EDR…` (Barclays), `OP…` (BNP), `SDB…` (GS), `EUN…` (Nomura), `nnnnn-nnnnn` (JPM), `Sop…` (HSBC), numeric (BAML/MS).
- [ ] **Manual MTM overrides**: the analyst sometimes replaces a broker mark with a different value (e.g. purchase price for settling trades — Marks sheet note "Used purchase price."). Found in 21 BNP "MCB" trades where the consolidated value matches no column of the raw statement. The system must detect and flag these (broker-statement value vs canonical value divergence) rather than assume the statement is always authoritative. `VERIFIED` (existence) / rule for when overrides are legitimate `TBD`.
- [x] **Long/Short share sign**: ALIC stores short positions as **negative shares**; notional and per-unit price follow that sign. Rule verified 251/253 vs ALIC: `shares = +N` for Long, `-N` for Short (broker "Client Direction"; traders DB `LongShort` is authoritative). Applied in `output/call_options_sheet.py`. `VERIFIED`
- [x] **"MCB" suffix = short legs** of double-legged trades. This one fact explains three earlier puzzles at once: these trades are negative-shares, carry the manual MTM overrides, and are the rows excluded by the positive-only `SUMIFS >0` subtotals (F7). `VERIFIED`
- [ ] **BNP short-leg-only rows**: 19 "MCB" rows report ref + dates + underlying + price + MTM but leave direction/quantity/strike/put-call blank. Parser captures ref+MTM correctly; notional can't be computed until the traders DB supplies quantity. `VERIFIED`

---

## 2. Traders Database Export Schema `VERIFIED` (field list provided by owner)

```
TradeNum, PAM_CUSIP, CounterpartyTradeID, EquityIndex, PutCall, Date_Expiry,
Strike, Notional, NumContract, LongShort, TotalPremium_LOCAL, TotalPremium_USD,
Counterparty, Date_Traded, Date_Settlement
```

- Traders DB = source of truth for trade population. `VERIFIED`
- Traders DB does **not** carry MTM; broker statements are the MTM source. `VERIFIED`
- Join keys to broker data: `CounterpartyTradeID` ↔ broker trade ref, plus `PAM_CUSIP` ↔ workbook Cusip. `INFERRED` — must be validated against a real export file. **No example traders-DB export file has been provided yet.** `TBD`

---

## 3. Target Architecture

```
                        ┌──────────────────────────────────────────────┐
 INPUTS (landing zone)  │  PROCESSING (deterministic batch pipeline)   │   OUTPUTS
┌─────────────────┐     │                                              │
│ broker_statements/ ───┼─► Broker Parser Registry (10 parsers)        │
│   <broker>/<YYYYMM>/  │      └─► normalized BrokerMark records       │
│ traders_export/    ───┼─► Traders DB Loader                          │
│ market_data/       ───┼─► Bloomberg Reference Loader (manual CSV;    │
│   (analyst-entered)   │      future: API adapter behind same iface)  │
└─────────────────┘     │                                              │
                        │  Matching Engine (trade-ref join, exceptions)│
                        │  Pricing Engine (F1–F5 rules)                │
                        │  Reconciliation Engine (derived vs broker)   │
                        │  Validation & Exception Framework            │
                        │                                              │   ┌──────────────────────┐
                        │  ──────────────────────────────────────────► │──►│ canonical_trades.csv  │
                        │                                              │   │ exceptions report     │
                        │  Audit log + lineage (every value traceable  │   │ reconciliation report │
                        │  to source file/cell/row)                    │   │ run manifest/audit log│
                        └──────────────────────────────────────────────┘   └──────────────────────┘
                                                                            (Phase 2: PAM messages)
```

Design principles:
- **Deterministic**: same inputs → byte-identical outputs. No network calls in the batch run. `VERIFIED` (requirement)
- **Explainable**: every output value carries lineage back to (source file, sheet, cell/row). `VERIFIED` (requirement)
- **Broker quirks are config/code per parser**, never special-cased in the core engine. `INFERRED` (design choice)

### 3.1 Technology stack `INFERRED` (proposed, owner to approve)

- Python 3.12 (installed, uv-managed)
- `pandas` + `openpyxl` / `xlrd<2.0` for Excel I/O; `pydantic` for schema validation; `pytest` for tests
- Storage: versioned CSVs + a per-run manifest under a structured data directory (no DB server needed for MVP; SQLite as Phase-2 upgrade path)
- Repo: this git repository (`jtanSE/derivative-monthly-pricing`)

---

## 4. Canonical Live-Trades Schema (draft v1)

One row per trade leg per month-end. `INFERRED` field set — assembled from the workbook columns, traders-DB fields, and requirements; review before freezing.

| Field | Source | Tag |
|---|---|---|
| `as_of_date` | run parameter (month-end) | `VERIFIED` |
| `trade_num` | traders DB `TradeNum` | `VERIFIED` |
| `pam_cusip` | traders DB `PAM_CUSIP` / workbook Cusip | `VERIFIED` |
| `counterparty_trade_id` | traders DB `CounterpartyTradeID` / broker trade ref | `VERIFIED` |
| `broker` | matched broker statement | `VERIFIED` |
| `equity_index_raw` / `equity_index_canonical` | traders DB + canonicalization map | `VERIFIED` (need) |
| `put_call` | traders DB `PutCall` (calls priced; puts flagged) | `VERIFIED` |
| `long_short` | traders DB `LongShort` | `VERIFIED` |
| `strike` | traders DB `Strike` | `VERIFIED` |
| `date_traded`, `date_expiry`, `date_settlement` | traders DB | `VERIFIED` |
| `currency` | workbook col F — origin in trader data `TBD` | `TBD` |
| `shares` | workbook col K — relationship to `NumContract`/`Notional` `TBD` | `TBD` |
| `index_price_monthend` | Bloomberg (analyst-entered) | `VERIFIED` |
| `fx_rate_to_usd` | Bloomberg (analyst-entered) | `VERIFIED` |
| `notional_local` (= shares × index price) | computed (F1) | `VERIFIED` |
| `notional_usd` (F2) | computed | `VERIFIED` |
| `mtm_broker_local` | broker statement (sign-normalized) | `VERIFIED` |
| `mtm_broker_usd` (F4) | computed | `VERIFIED` |
| `mtm_derived_local` / `mtm_derived_usd` | pricing engine | `VERIFIED` (owner decision) / formula `TBD` |
| `mtm_source` | `'broker'` default; metadata for valuation view | `VERIFIED` (owner decision) |
| `price_per_unit_local` (F5) | computed | `VERIFIED` |
| `portfolio_118_alloc`, `portfolio_119_alloc`, `alloc_total`, `alloc_diff` | PFI extract (F9) — source feed `TBD` | `TBD` |
| `trade_status` | `live` / `expired_unsettled` / `expired_settled` (drives archiving) | `VERIFIED` (requirement) |
| `match_status`, `match_confidence` | matching engine | `VERIFIED` (requirement) |
| `recon_status`, `recon_diff_local`, `recon_tolerance` | reconciliation engine | `VERIFIED` (requirement) / tolerance `TBD` |
| `lineage_*` (source file, sheet, row per sourced value) | ingestion layer | `VERIFIED` (requirement) |

- [ ] Review and freeze canonical schema v1
- [ ] Define puts handling: rows ingested, `recon_status='out_of_scope_mvp'`

---

## 5. Implementation Phases & Checklist

### Phase 0 — Repo & project scaffolding
- [x] `pyproject.toml` (uv-managed), package layout `src/derivative_pricing/`
- [x] `.gitignore` (exclude real statement data; keep sanitized fixtures only)
- [x] Data landing-zone directory convention: `data/templates/` seeded; per-month dirs git-ignored
- [x] Test harness (`pytest`) — 23 tests passing, incl. parallel-run gate vs the real workbook
- [ ] Decision: confirm proposed tech stack (§3.1) — *implemented as proposed; owner to ratify*

### Phase 1 — Reference data & canonical schema
- [x] Canonical schema module (pydantic models) per §4 — `schema.py`
- [x] Index canonicalization map (ticker ↔ names ↔ variants; seeded from workbook W/X table + col B variants) — `reference/indices.py`
- [x] FX/market-data input format for analyst manual entry (CSV templates mirroring T:U and V:X tables) — `data/templates/`
- [x] Bloomberg adapter interface (manual-CSV implementation now; API later behind same `MarketData` seam) — `reference/market_data.py`

### Phase 2 — Broker parser layer (one broker at a time, owner-confirmed order `TBD`)
Parser contract: `parse(file) -> list[BrokerMark]` with `broker, trade_ref, mtm_local, [currency, extra fields]`, plus per-row lineage.
- [x] Parser registry + common normalized output model — `parsers/base.py`, `BrokerMark` with `extra` dict for rich fields
- [x] Sign-convention config per broker (Barclays + Nomura flip) — **applied in parser layer, not core**
- [x] SUPBOOK-tolerant `.xls` reader for broker files with external-link records — `parsers/xls_compat.py`
- [ ] Barclays parser + golden-file test
- [x] BNP Paribas parsing — now via `adaptive/specs/bnp.json` + engine (the hand-written `parsers/bnp.py` was removed once the spec reproduced it byte-identical). Reads sheet `Option - Reg. MM`, MTM from column P "Mark to Market (N*P)"; captures rich fields (dates, underlying, Bloomberg code, strike, qty). 251/272 marks agree with the March Marks sheet to the cent; the 21 "MCB" differences are confirmed analyst manual overrides ("Used purchase price"), to be surfaced by reconciliation. Needs SUPBOOK-tolerant `.xls` open (`parsers/xls_compat.py`).
- [ ] Morgan Stanley parser + test
- [ ] Goldman Sachs parser + test
- [ ] Wells Fargo parser + test
- [ ] BAML parser + test
- [ ] Credit Suisse parser + test
- [ ] Nomura parser + test
- [ ] JP Morgan parser + test
- [ ] HSBC parser + test
- [ ] **Blocked**: need at least one real (sanitized) statement file per broker — only the consolidated `Marks` paste exists today. `TBD`

### Phase 3 — Traders DB ingestion & live-trades maintenance
- [ ] Loader for traders DB Excel export (15-field schema §2) — **needs example export file** `TBD`
- [ ] Live/expired/settled classification (`Date_Expiry`, `Date_Settlement` vs as-of date)
- [ ] Auto-add new trades; update existing; archive expired+settled
- [ ] Calls priced; puts ingested + flagged (owner decision)

### Phase 4 — Matching engine
- [x] Join broker marks ↔ trader trades on trade ref (primary) — `matching/engine.py`
- [ ] Fallback candidate keys: cusip, counterparty + index + expiry + strike + shares — *needs traders export (Q1)*
- [x] Match confidence levels (exact / candidate / unmatched / ambiguous) — enum + outcome buckets
- [x] Exception queue: unmatched marks/trades flagged, never dropped — covered by tests
- [ ] Double-legged trade pairing logic — **business rule `TBD`** (offsetting ± leg pairs observed; need owner walkthrough of one real example)

### Phase 5 — Pricing & valuation engine
- [x] F1: notional_local = shares × index_price (unified price table) — `pricing/engine.py`, validated vs workbook
- [x] F2/F4: USD conversion = ROUND(x × fx, 2) — Excel half-away-from-zero ROUND replicated exactly
- [x] F5: per-unit price = mtm_local / shares — validated vs workbook
- [ ] Derived MTM calculation for validation — **exact formula/inputs `TBD`** (intrinsic? premium-based? owner to specify)
- [ ] Formula-variation hooks by trade/product/index/currency (requirement; concrete variations `TBD`)
- [ ] Valuation view metadata (`broker` / `internal` / `client`) on every MTM value

### Phase 6 — Reconciliation & validation
- [x] Derived vs broker MTM comparison, per-trade diff — `recon/engine.py` (`reconcile_mtm`)
- [ ] Tolerance framework — **thresholds `TBD`** (absolute? bps of notional? per-broker?) — *takes threshold as explicit param; default unset*
- [x] Per-broker subtotals replicating F7 positive-only SUMIFS semantics — `section_totals` (the *why* is still Q8)
- [ ] Cross-check totals (F10) once rows 1544/1545 are identified `TBD`
- [ ] Recon report (Excel/CSV) for Director of Operations review
- [ ] Month-over-month consistency checks (trade count drift, stale marks)

### Phase 7 — Outputs, audit, lineage
- [x] ALIC-style "Call Options" sheet writer (`.xlsx` + `.csv`) — `output/call_options_sheet.py`. Reproduces the analyst's layout and derived columns; BNP output matches ALIC on Notional/Price for 250/272 trades (rest = manual overrides).
- [ ] `canonical_trades_<YYYYMM>.csv` writer (full canonical schema, post-matching)
- [ ] Exception report writer
- [ ] Run manifest: input file hashes, run timestamp, code version, record counts
- [ ] Value-level lineage (source file/sheet/row for every sourced number)
- [ ] Parallel-run harness: compare system output vs analyst workbook for the same month (acceptance gate)

### Phase 8+ — Future (explicitly out of MVP)
- [ ] PAM message builder (consumes canonical CSV; needs PAM spec + portfolio 118/119 feed decision)
- [ ] Put-option pricing activation
- [ ] Bloomberg API adapter
- [ ] Broker confirmation PDF parsing
- [ ] SQLite/DB storage upgrade

---

## 8. Adaptive Parsing for New / Unknown Brokers (design — not yet built)

**Goal.** Parse statements from brokers we have not hand-coded (including
brand-new counterparties) into the canonical `BrokerMark` shape, without
writing a fresh Python parser every time.

**Core principle — LLM at onboarding, deterministic at runtime.**
An LLM is used *once per new format* to propose a column **mapping**, not every
month to read the data. A human approves the mapping; from then on plain
deterministic code runs it forever. The LLM never touches a monthly number —
only the structure, once. This preserves determinism, auditability, and
reproducibility (the same controls the whole platform requires).

**Representation — hybrid (owner decision).**
- Most brokers: a **declarative mapping spec** (data, not code: sheet, header
  anchor, column→field map, sign rules, row filters) executed by one shared,
  well-tested generic engine. A new broker = a ~15-line form, not a program.
- Genuinely weird layouts (e.g. the 10-section stacked ALIC sheet): keep the
  **Python escape hatch** — a hand-written parser registered via
  `parsers.register_parser` into `PARSER_REGISTRY`.
- Both emit identical `BrokerMark` records, so the rest of the system is
  agnostic to which was used.

**Sequencing — build examples before abstracting (the "rule of three").**
Do **not** design the generic engine/spec from a single parser; it would overfit
to one broker's quirks. Hand-write 3–4 deterministic parsers spanning real
format diversity first, *then* extract the spec schema from what actually
varies. Those 3–4 parsers double as the few-shot examples the LLM needs.

- [x] Reference formats #1 BNP + #2 BAML — first hand-written to learn the variety, then expressed as specs (`adaptive/specs/{bnp,baml}.json`); the engine reproduced both byte-identical, after which the hand-written parsers were removed (now spec-only).
- [x] Spec schema + generic engine (`adaptive/spec.py`, `adaptive/engine.py`) — covers all reference formats; correctness checked against the analyst's Marks sheet
- [x] AI mapping-proposer scaffolding (`adaptive/agent.py`): fingerprint → few-shot prompt → spec; deterministic `validate_spec` gate. (Live API call needs `ANTHROPIC_API_KEY`; absent, a human/LLM authors the spec JSON.)
- [x] **Held-out test — Goldman Sachs**: a GS spec drafted from only the BNP/BAML examples + the GS fingerprint parsed all 310 trades and matched the analyst's Marks sheet **310/310 to the cent**. Strong evidence the approach generalizes.
- [x] **Held-out test #4 — HSBC** (`adaptive/specs/hsbc.json`): a 4th distinct format — and the first **`.xlsx`**. Forced two real generalizations: a format-agnostic reader (`adaptive/reader.py`, xls+xlsx) and exact-header disambiguation ("MtM" vs "MtM Ccy"/"MtM Less Accrued Interest"). Spec drafted from the prior examples matched the analyst's Marks sheet **18/18 to the cent**.
- [x] **Format router** (`adaptive/router.py`): `identify()`/`route()`/`route_folder()` recognize a known file by its spec signature (header anchor + optional sheet name) and run it deterministically; unknown files are flagged for the learning agent, ambiguous ones for a human. Verified: all 4 brokers auto-identified; the ALIC workbook correctly flagged unknown.
- [x] Spec persistence + onboarding flow (`agent.save_spec`, `agent.onboard_file`): a new broker becomes permanently known once its validated spec is saved to `specs/`. `onboard_file` is the end-to-end path — known → no-op; unknown → draft → validate → (on approval) save. Approval is required before save (no auto-trust); `save_spec` refuses to overwrite.
- [ ] Wire live `propose_spec` API call + prompt caching once a key is available (the draft step is the only part still needing the key)
- [ ] Human review/approval UI: proposed mapping + sample extracted values beside raw cells
- [ ] Confidence scoring on proposed specs (persistence + approval gate already in place)
- [x] Removed redundant hand-written `bnp.py`/`baml.py` (specs cover them); escape-hatch mechanism retained via `register_parser`/`PARSER_REGISTRY`

**Why few-shot, not fine-tuning.** In-context examples from our approved specs
are cheaper, simpler, and easier to audit/update than a fine-tuned model.
Fine-tuning is only a later optimization if volume ever justifies it.

**Controls (institutional).** New-broker mappings are never auto-approved;
human-in-the-loop is mandatory for first use. The LLM proposes structure only;
all monetary extraction is deterministic. Lineage is preserved end-to-end. A
mis-mapped column degrades to a validation failure or a visible review item,
never a silent bad number.

---

## 6. Open Questions (blocking items marked ⛔)

| # | Question | Blocks |
|---|---|---|
| Q1 ⛔ | Need example **traders DB export file** to validate schema §2 and join keys | Phase 3, 4 |
| Q2 ⛔ | Need per-broker **raw statement files** (sanitized fine) — `Marks` is already-consolidated data | Phase 2 |
| Q3 ⛔ | **Reconciliation tolerance thresholds** (per broker? absolute vs bps?) | Phase 6 |
| Q4 | **Derived MTM formula**: what inputs/method should the validation MTM use? | Phase 5 |
| Q5 | **Multi-leg trades**: business rule for hand-summed Shares and ± leg pairs (one real example walkthrough) | Phase 4 |
| Q6 | **Portfolio 118/119 allocations**: what feed provides them going forward (Sheet3 came from PFI)? What is the scrubbed lookup key in F9? | Phase 7/8, schema |
| Q7 | What are rows **1544/1545** in the J-column cross-check (F10)? | Phase 6 |
| Q8 | Why are notional/shares subtotals **positive-only** (`SUMIFS >0`) — short-position exclusion convention? | Phase 6 |
| Q9 | `currency` and `shares` provenance from trader data (`NumContract` × multiplier? separate field?) | Schema §4 |
| Q10 | Broker parser build **order** (suggest starting with Barclays = largest book, or a small one like Credit Suisse for fast end-to-end) | Phase 2 |

---

## 7. Acceptance Criteria for MVP

- [ ] For a given month, system ingests all 10 broker statements + traders export + analyst market-data CSV and produces the canonical CSV **without manual intervention**.
- [ ] Parallel run vs the analyst's workbook matches all `VERIFIED` computed columns (G, H, I, J, L equivalents) within float tolerance, for every matched trade.
- [ ] Every unmatched/out-of-tolerance item appears in the exception report with a reason code — zero silent drops.
- [ ] Re-running the same inputs produces byte-identical outputs.
- [ ] Every number in the canonical CSV is traceable to a source file/cell or a named formula rule (F1–F11).