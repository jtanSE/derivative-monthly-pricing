"""Bloomberg Data License (HAPI) market-data provider.

DETERMINISM CONTRACT (plan section 7): this module *produces* a frozen
month-end snapshot; it is NEVER called from the pricing path. Pricing reads the
frozen CSVs via ``load_market_data``. A correction creates a NEW snapshot
version; existing snapshots are immutable. Re-running an audit two years later
reproduces the same numbers because it reads the frozen file, not a live feed.

The provider seam
-----------------
``MarketDataProvider`` is a Protocol. Anything that can return quotes for a set
of Bloomberg securities satisfies it:

- ``BloombergHapiProvider`` -- the real Data License REST client (server-side,
  OAuth2, runs unattended in the container).
- ``FakeProvider`` -- deterministic canned quotes for tests and dry runs
  (``snapshot ... --fake``), so the whole pipeline is exercisable with no
  credentials and no network.

WHAT IS VALIDATED vs WHAT NEEDS YOUR CATALOG
--------------------------------------------
Unit tests cover the request-payload builders and the response parser against
representative fixtures, plus the full snapshot flow via ``FakeProvider``. Three
things depend on your specific DL account and must be confirmed live (each is
isolated in one small method, marked ``# CONFIRM:``):
  1. the OAuth2 token endpoint URL,
  2. the one-shot trigger name for a point-in-time pull,
  3. the exact JSON shape of the response payload.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SecurityRequest:
    """One Bloomberg security + field to request."""

    identifier: str  # yellow-key security, e.g. "SPX Index", "EURUSD Curncy"
    field: str = "PX_LAST"


@dataclass(frozen=True)
class Quote:
    """One returned value. ``value`` is None when Bloomberg had no datum."""

    identifier: str
    field: str
    value: float | None


class MarketDataProvider(Protocol):
    """Anything that can resolve securities -> quotes for a given as-of date."""

    def fetch(self, securities: list[SecurityRequest], as_of: str) -> list[Quote]:
        ...


# ---------------------------------------------------------------------------
# Fake provider: deterministic, offline. Used by tests and `--fake` dry runs.
# ---------------------------------------------------------------------------
class FakeProvider:
    """Returns canned values from an ``identifier -> value`` map.

    Unknown identifiers come back with ``value=None`` (mirrors a real "no data"
    response) so the snapshot layer's missing-data handling is exercised.
    """

    def __init__(self, values: dict[str, float]):
        self._values = dict(values)

    def fetch(self, securities: list[SecurityRequest], as_of: str) -> list[Quote]:
        return [
            Quote(s.identifier, s.field, self._values.get(s.identifier))
            for s in securities
        ]


# ---------------------------------------------------------------------------
# Bloomberg HAPI configuration (server-side credentials, from the environment).
# Mirrors the container secret pattern (docker-compose ${VAR:-}); never hard-code.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BloombergHapiConfig:
    client_id: str
    client_secret: str
    catalog: str  # your Data License catalog id
    # CONFIRM: token endpoint for your DL account (Bloomberg SSO).
    token_url: str = "https://bsso.bloomberg.com/ext/api/as/token.oauth2"
    api_host: str = "https://api.bloomberg.com"
    api_version: str = "3"
    # CONFIRM: the trigger that runs a request once, immediately.
    trigger: str = "oneshot"
    poll_interval_s: float = 10.0
    poll_timeout_s: float = 1800.0  # 30 min; DL responses are not instant

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BloombergHapiConfig:
        e = env if env is not None else os.environ
        missing = [
            k for k in ("BLOOMBERG_HAPI_CLIENT_ID", "BLOOMBERG_HAPI_CLIENT_SECRET",
                        "BLOOMBERG_HAPI_CATALOG") if not e.get(k)
        ]
        if missing:
            raise RuntimeError(
                "Bloomberg HAPI credentials missing from environment: "
                + ", ".join(missing)
                + ". Set them via your secrets manager / host environment "
                  "(never commit them)."
            )
        return cls(
            client_id=e["BLOOMBERG_HAPI_CLIENT_ID"],
            client_secret=e["BLOOMBERG_HAPI_CLIENT_SECRET"],
            catalog=e["BLOOMBERG_HAPI_CATALOG"],
            token_url=e.get("BLOOMBERG_HAPI_TOKEN_URL", cls.token_url),
            api_host=e.get("BLOOMBERG_HAPI_HOST", cls.api_host),
            trigger=e.get("BLOOMBERG_HAPI_TRIGGER", cls.trigger),
        )


class _Http:
    """Tiny seam over the network so the flow is mockable in tests.

    ``requests`` is imported lazily and only lives in the optional ``bloomberg``
    dependency group -- the core package and its tests need no network library.
    """

    def __init__(self, token: str, api_version: str):
        import requests  # lazy: optional dependency

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "api-version": api_version,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def post(self, url: str, json: dict) -> dict:
        r = self._session.post(url, json=json, timeout=60)
        r.raise_for_status()
        # HAPI returns the created resource id in Location and/or the body.
        if r.headers.get("Location"):
            return {"@id": r.headers["Location"], **(_safe_json(r))}
        return _safe_json(r)

    def get(self, url: str) -> dict:
        r = self._session.get(url, timeout=60)
        r.raise_for_status()
        return _safe_json(r)


def _safe_json(response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}


class BloombergHapiProvider:
    """Bloomberg Data License Per-Security REST (HAPI) client.

    Flow: OAuth2 token -> create universe -> create fieldList -> submit request
    -> poll responses -> download -> parse. Every HTTP call goes through a small
    ``_Http`` seam, and the payload builders / response parser are pure so they
    are unit-tested without a live account.
    """

    def __init__(self, config: BloombergHapiConfig, http: _Http | None = None,
                 sleep=time.sleep):
        self._cfg = config
        self._http = http  # injected in tests; built from a token otherwise
        self._sleep = sleep

    # --- token -------------------------------------------------------------
    def _authenticate(self) -> _Http:
        if self._http is not None:
            return self._http
        import requests  # lazy: optional dependency

        resp = requests.post(
            self._cfg.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._cfg.client_id,
                "client_secret": self._cfg.client_secret,
            },
            timeout=60,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        return _Http(token, self._cfg.api_version)

    # --- payload builders (pure; unit-tested) ------------------------------
    def _catalog_base(self) -> str:
        return f"{self._cfg.api_host}/eap/catalogs/{self._cfg.catalog}"

    def build_universe_payload(self, securities: list[SecurityRequest],
                               name: str) -> dict:
        return {
            "@type": "Universe",
            "identifier": name,
            "title": f"month-end market data {name}",
            "contains": [
                {
                    "@type": "Identifier",
                    "identifierType": "TICKER",
                    "identifierValue": s.identifier,
                }
                for s in securities
            ],
        }

    def build_fieldlist_payload(self, securities: list[SecurityRequest],
                                name: str) -> dict:
        fields = sorted({s.field for s in securities})
        return {
            "@type": "DataFieldList",
            "identifier": name,
            "title": f"fields {name}",
            "contains": [{"mnemonic": f} for f in fields],
        }

    def build_request_payload(self, universe_id: str, fieldlist_id: str,
                              name: str, as_of: str) -> dict:
        base = self._catalog_base()
        return {
            "@type": "DataRequest",
            "identifier": name,
            "title": f"month-end pull {as_of}",
            "universe": universe_id,
            "fieldList": fieldlist_id,
            "trigger": f"{base}/triggers/{self._cfg.trigger}/",
            "formatting": {"@type": "MediaType", "outputMediaType": "application/json"},
            # Point-in-time close on the as-of date (not "live"): a single-date
            # historical range makes the pull reproducible.
            # CONFIRM: exact runtimeOptions shape for your catalog.
            "runtimeOptions": {
                "@type": "HistoricalDataRuntimeOptions",
                "historicalDataRange": {"@type": "DateRange", "startDate": as_of, "endDate": as_of},
            },
        }

    # --- response parser (pure; unit-tested) -------------------------------
    @staticmethod
    def parse_response(payload: dict) -> list[Quote]:
        """Map a HAPI JSON response to Quotes.

        CONFIRM: shape against a real DL response. This handles the documented
        per-security JSON layout; adjust the field paths here if your catalog
        differs -- nothing else in the client needs to change.
        """
        quotes: list[Quote] = []
        for record in payload.get("data", []):
            ident = record.get("identifier") or record.get("security")
            fields = record.get("fields") or record.get("data") or {}
            for field_name, value in fields.items():
                num = _as_float(value)
                quotes.append(Quote(ident, field_name, num))
        return quotes

    # --- orchestration -----------------------------------------------------
    def fetch(self, securities: list[SecurityRequest], as_of: str) -> list[Quote]:
        http = self._authenticate()
        base = self._catalog_base()
        # A stable, run-specific name; caller varies it per as-of.
        stamp = f"me{as_of.replace('-', '')}"

        universe = http.post(f"{base}/universes/",
                             self.build_universe_payload(securities, f"u{stamp}"))
        fieldlist = http.post(f"{base}/fieldLists/",
                             self.build_fieldlist_payload(securities, f"f{stamp}"))
        request = http.post(
            f"{base}/requests/",
            self.build_request_payload(universe["@id"], fieldlist["@id"],
                                       f"r{stamp}", as_of),
        )
        payload = self._poll_response(http, base, request, stamp)
        return self.parse_response(payload)

    def _poll_response(self, http: _Http, base: str, request: dict,
                       stamp: str) -> dict:
        deadline = self._cfg.poll_timeout_s
        waited = 0.0
        while waited <= deadline:
            listing = http.get(f"{base}/content/responses/?prefix=r{stamp}")
            for item in listing.get("contains", []):
                key = item.get("key") or item.get("identifier", "")
                if stamp in key:
                    return http.get(item["@id"])
            self._sleep(self._cfg.poll_interval_s)
            waited += self._cfg.poll_interval_s
        raise TimeoutError(
            f"Bloomberg HAPI response for r{stamp} not ready after "
            f"{deadline:.0f}s"
        )


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
