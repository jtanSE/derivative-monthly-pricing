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

## Pre-loading statements (optional)

By default users upload statements in the browser. To pre-load a folder
instead, mount it and point `STATEMENTS_DIR` at it:

```bash
docker run -p 8501:8501 \
  -v /path/to/statements:/data/statements:ro \
  -e STATEMENTS_DIR=/data/statements \
  derivative-monthly-pricing
```

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
