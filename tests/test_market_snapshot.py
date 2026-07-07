"""Market-data snapshot: FX conventions, missing data, immutability, and the
HAPI payload/parse seams. Everything runs offline via FakeProvider -- no
credentials, no network."""

import datetime as dt
import json

import pytest

from derivative_pricing.reference.bloomberg import (
    BloombergHapiConfig,
    BloombergHapiProvider,
    FakeProvider,
    Quote,
    SecurityRequest,
)
from derivative_pricing.reference.market_data import load_market_data
from derivative_pricing.reference.market_snapshot import (
    FxSecurity,
    IndexSecurity,
    MarketDataUniverse,
    build_snapshot,
    freeze_snapshot,
)

FIXED_PULL = dt.datetime(2026, 4, 1, 2, 0, 0, tzinfo=dt.timezone.utc)


def _universe():
    return MarketDataUniverse(
        indices=[
            IndexSecurity("S&P 500", "SPX Index"),
            IndexSecurity("Russell 2000", "RTY Index"),
        ],
        fx=[
            FxSecurity(currency="USD", fixed_rate=1.0),
            FxSecurity(currency="EUR", identifier="EURUSD Curncy", convention="direct"),
            FxSecurity(currency="JPY", identifier="USDJPY Curncy", convention="inverse"),
        ],
    )


def test_direct_and_inverse_fx_conventions():
    provider = FakeProvider({
        "SPX Index": 5670.97,
        "RTY Index": 2100.5,
        "EURUSD Curncy": 1.1553,   # USD per EUR -> rate_to_usd = 1.1553
        "USDJPY Curncy": 150.0,    # JPY per USD -> rate_to_usd = 1/150
    })
    snap = build_snapshot(_universe(), provider, "2026-03-31", pulled_at=FIXED_PULL)

    assert snap.fx_rates["USD"] == 1.0
    assert snap.fx_rates["EUR"] == pytest.approx(1.1553)
    assert snap.fx_rates["JPY"] == pytest.approx(1.0 / 150.0)
    assert snap.index_prices["S&P 500"] == pytest.approx(5670.97)
    assert not snap.missing


def test_missing_data_is_flagged_not_faked():
    provider = FakeProvider({"SPX Index": 5670.97})  # RTY, EUR, JPY absent
    snap = build_snapshot(_universe(), provider, "2026-03-31", pulled_at=FIXED_PULL)

    assert "S&P 500" in snap.index_prices
    assert "index:Russell 2000" in snap.missing
    assert "fx:EUR" in snap.missing
    assert "fx:JPY" in snap.missing
    # USD fixed rate is always present
    assert snap.fx_rates["USD"] == 1.0
    # nothing invented for the missing ones
    assert "EUR" not in snap.fx_rates


def test_freeze_writes_csvs_that_load_market_data_reads(tmp_path):
    provider = FakeProvider({
        "SPX Index": 5670.97, "RTY Index": 2100.5,
        "EURUSD Curncy": 1.1553, "USDJPY Curncy": 150.0,
    })
    snap = build_snapshot(_universe(), provider, "2026-03-31", pulled_at=FIXED_PULL)
    freeze_snapshot(snap, tmp_path)

    # The frozen CSVs are exactly what the pricing pipeline consumes.
    md = load_market_data("2026-03-31", tmp_path / "fx.csv", tmp_path / "index.csv")
    assert md.fx("EUR") == pytest.approx(1.1553)
    assert md.fx("JPY") == pytest.approx(1.0 / 150.0)
    assert md.index_price("S&P 500") == pytest.approx(5670.97)


def test_snapshot_json_carries_full_lineage(tmp_path):
    provider = FakeProvider({
        "SPX Index": 5670.97, "RTY Index": 2100.5,
        "EURUSD Curncy": 1.1553, "USDJPY Curncy": 150.0,
    })
    snap = build_snapshot(_universe(), provider, "2026-03-31", pulled_at=FIXED_PULL)
    path = freeze_snapshot(snap, tmp_path)

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["as_of"] == "2026-03-31"
    assert doc["pulled_at_utc"] == "2026-04-01T02:00:00+00:00"
    # every JPY record records the inverse conversion for the auditor
    jpy = [l for l in doc["lineage"] if l["key"] == "JPY"][0]
    assert jpy["raw_quote"] == 150.0
    assert jpy["convention"] == "inverse"
    assert jpy["resolved_value"] == pytest.approx(1.0 / 150.0)


def test_snapshot_is_immutable_without_overwrite(tmp_path):
    provider = FakeProvider({"SPX Index": 5670.97})
    snap = build_snapshot(_universe(), provider, "2026-03-31", pulled_at=FIXED_PULL)
    freeze_snapshot(snap, tmp_path)

    # A second freeze into the same dir must refuse (protect the audit trail)...
    with pytest.raises(FileExistsError):
        freeze_snapshot(snap, tmp_path)
    # ...unless a correction is explicit.
    freeze_snapshot(snap, tmp_path, overwrite=True)


def test_bootstrap_universe_from_canonical_indices():
    uni = MarketDataUniverse.bootstrap_from_indices()
    names = {ix.index_name for ix in uni.indices}
    assert "S&P 500" in names
    # indices with no known ticker are skipped, not guessed
    assert "US Balanced Asset 10" not in names
    # every bootstrapped index carries a yellow-key identifier
    assert all(ix.identifier.endswith(" Index") for ix in uni.indices)
    assert uni.fx[0].currency == "USD" and uni.fx[0].fixed_rate == 1.0


def test_universe_from_json_matches_template():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    uni = MarketDataUniverse.from_json(repo / "data/templates/market_universe.json")
    assert any(ix.index_name == "S&P 500" for ix in uni.indices)
    jpy = [f for f in uni.fx if f.currency == "JPY"][0]
    assert jpy.convention == "inverse"


# --- HAPI client seams (pure builders + parser; no network) ----------------
def _provider():
    cfg = BloombergHapiConfig(client_id="x", client_secret="y", catalog="cat")
    return BloombergHapiProvider(cfg)


def test_hapi_universe_payload_shape():
    p = _provider()
    payload = p.build_universe_payload(
        [SecurityRequest("SPX Index"), SecurityRequest("EURUSD Curncy")], "u1")
    assert payload["@type"] == "Universe"
    values = [c["identifierValue"] for c in payload["contains"]]
    assert values == ["SPX Index", "EURUSD Curncy"]


def test_hapi_request_payload_is_point_in_time():
    p = _provider()
    req = p.build_request_payload("u/1", "f/1", "r1", "2026-03-31")
    # a single-date historical range makes the pull reproducible
    rng = req["runtimeOptions"]["historicalDataRange"]
    assert rng["startDate"] == "2026-03-31" == rng["endDate"]
    assert req["universe"] == "u/1" and req["fieldList"] == "f/1"


def test_hapi_response_parser():
    payload = {"data": [
        {"identifier": "SPX Index", "fields": {"PX_LAST": "5670.97"}},
        {"identifier": "EURUSD Curncy", "fields": {"PX_LAST": 1.1553}},
        {"identifier": "BAD Index", "fields": {"PX_LAST": None}},
    ]}
    quotes = BloombergHapiProvider.parse_response(payload)
    by_id = {q.identifier: q.value for q in quotes}
    assert by_id["SPX Index"] == pytest.approx(5670.97)
    assert by_id["EURUSD Curncy"] == pytest.approx(1.1553)
    assert by_id["BAD Index"] is None


def test_hapi_config_from_env_requires_credentials():
    with pytest.raises(RuntimeError) as exc:
        BloombergHapiConfig.from_env({})
    assert "BLOOMBERG_HAPI_CLIENT_ID" in str(exc.value)

    cfg = BloombergHapiConfig.from_env({
        "BLOOMBERG_HAPI_CLIENT_ID": "id",
        "BLOOMBERG_HAPI_CLIENT_SECRET": "secret",
        "BLOOMBERG_HAPI_CATALOG": "cat",
    })
    assert cfg.catalog == "cat"


def test_hapi_fetch_end_to_end_with_mocked_http():
    """Drive the full orchestration (universe->fieldlist->request->poll->parse)
    through a fake _Http, proving the flow wires together without a network."""
    cfg = BloombergHapiConfig(client_id="x", client_secret="y", catalog="cat",
                              poll_interval_s=0, poll_timeout_s=1)

    class FakeHttp:
        def __init__(self):
            self.posts = []

        def post(self, url, json):
            self.posts.append(url)
            # echo back a resource id per created resource
            return {"@id": url + "created"}

        def get(self, url):
            if url.endswith("/file"):
                # the downloaded response file
                return {"data": [{"identifier": "SPX Index", "fields": {"PX_LAST": 5670.97}}]}
            # listing: point at a ready response
            return {"contains": [{"key": "rme20260331", "@id": url + "/file"}]}

    provider = BloombergHapiProvider(cfg, http=FakeHttp(), sleep=lambda s: None)
    quotes = provider.fetch([SecurityRequest("SPX Index")], "2026-03-31")
    assert quotes[0].identifier == "SPX Index"
    assert quotes[0].value == pytest.approx(5670.97)
