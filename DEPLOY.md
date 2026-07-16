# Deployment

The app is a single Streamlit process that serves both the UI and the parsing
backend. For multiple users, host it once internally; users just open a URL —
no install, no Python, no CLI on their side.

## Build & run (Docker)

```bash
# build
docker build -t derivative-monthly-pricing .

# run (key-free; fully functional for all known brokers)
docker run -p 8501:8501 derivative-monthly-pricing
# → open http://<host>:8501
```

Or with compose:

```bash
docker compose up --build
```

## The optional API key (server-side, one key, not per-user)

Needed **only** for self-service onboarding of a brand-new broker (the agent
drafting a spec). End users never supply a key. Set it once on the server:

```bash
docker run -p 8501:8501 -e ANTHROPIC_API_KEY=sk-... derivative-monthly-pricing
```

Prefer a secrets manager / orchestrator secret over a plain env var in
production. Without the key, onboarding a new broker falls back to a human
authoring its spec JSON; everything else is unaffected.

### Kill switch — turning the AI parser off

The model is used **only** to draft a spec for a brand-new broker format (a
rare, one-time event); the monthly deterministic pipeline never calls it. To
turn that AI path off entirely — e.g. a spend guardrail — set:

```bash
docker run -p 8501:8501 -e AI_ONBOARDING_ENABLED=false derivative-monthly-pricing
```

Unset or empty = enabled (the default). When disabled, unknown formats return a
clear "AI onboarding is disabled" result and must be hand-authored as
`specs/<broker>.json`; **parsing of already-known brokers is unaffected.**
For a hard backstop independent of the app, also set a workspace spend limit on
the Anthropic API key in the Console.

## Pre-loading statements (optional)

By default users upload statements in the browser. To pre-load a folder
instead, mount it and point `STATEMENTS_DIR` at it:

```bash
docker run -p 8501:8501 \
  -v /path/to/statements:/data/statements:ro \
  -e STATEMENTS_DIR=/data/statements \
  derivative-monthly-pricing
```

## Month-end market data (Bloomberg Data License / HAPI)

Index prices and FX rates are pulled from Bloomberg **once per month-end,
frozen to disk, and then read by the pricing run**. Bloomberg is never called
during pricing — so re-running any past month in an audit reproduces the exact
same numbers from the frozen snapshot, not a moved live feed.

```bash
# 1) freeze the snapshot (server-side, credentials from the environment)
BLOOMBERG_HAPI_CLIENT_ID=...  BLOOMBERG_HAPI_CLIENT_SECRET=...  BLOOMBERG_HAPI_CATALOG=... \
  python -m derivative_pricing.snapshot_market_data \
    --as-of 2026-03-31 --out data/2026-03-31 \
    --universe data/templates/market_universe.json
#   → writes data/2026-03-31/{index.csv, fx.csv, snapshot.json}

# 2) price against the frozen snapshot
python -m derivative_pricing.run_month <statements> --as-of 2026-03-31 \
    --fx data/2026-03-31/fx.csv --index data/2026-03-31/index.csv
```

- **Dry run without credentials:** add `--fake` to step 1 to exercise the full
  wiring with canned values (no network, no Bloomberg entitlement).
- **The universe** (`data/templates/market_universe.json`) lists which
  Bloomberg securities to pull — public tickers only, safe to commit. FX
  `convention` is `direct` for `XXXUSD` pairs and `inverse` for `USDXXX`.
- **Snapshots are immutable:** re-freezing the same as-of refuses unless you
  pass `--overwrite` (a deliberate correction, with its own pull timestamp).
- **`snapshot.json` is the audit record:** every value carries its ticker,
  field, raw quote, and conversion.
- Frozen snapshots live under `data/<as-of>/`, which `.gitignore` keeps private.

> Three account-specific details in `reference/bloomberg.py` must be confirmed
> against your DL catalog before the first live pull (each marked `# CONFIRM:`):
> the OAuth2 token URL, the one-shot trigger name, and the response JSON shape.
> They are isolated in unit-tested methods, so finalizing them is a small edit.

## Data handling (important for an audited environment)

- **No broker data is baked into the image** (`.dockerignore` excludes
  `*.xls`/`*.xlsx`/`data/`). Build context never carries counterparty data.
- Uploaded files are written to the container's temp dir for parsing. For
  production, mount a dedicated volume for temp/working files and set a
  retention/cleanup policy.
- Host **inside the corporate network**; do not expose the port publicly.

## Still to add for a true production app (not built yet)

- **Authentication** — put the app behind your SSO / reverse proxy (e.g.
  internal load balancer + OIDC), or Streamlit auth. The app itself has no
  login today.
- **Audit logging** — record who ran which month-end, which files, and the
  output produced (the original requirements call for auditability).
- **Persistent spec store** — `src/derivative_pricing/adaptive/specs/` is baked
  into the image; newly onboarded specs written at runtime need a mounted
  volume to survive restarts.
