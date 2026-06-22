"""Router: known-file recognition vs unknown-file hand-off."""

from derivative_pricing.adaptive.router import identify, route, route_folder
from derivative_pricing.schema import Broker


def test_identifies_known_brokers(bnp_statement_path, baml_statement_path,
                                  gs_statement_path, hsbc_statement_path):
    cases = [
        (bnp_statement_path, Broker.BNP_PARIBAS),
        (baml_statement_path, Broker.BAML),
        (gs_statement_path, Broker.GOLDMAN_SACHS),
        (hsbc_statement_path, Broker.HSBC),
    ]
    for path, broker in cases:
        matches = identify(path)
        assert len(matches) == 1, f"{path.name}: {[m.broker.value for m in matches]}"
        assert matches[0].broker == broker


def test_route_known_parses_deterministically(gs_statement_path):
    r = route(gs_statement_path)
    assert r.status == "known"
    assert r.broker == "Goldman Sachs"
    assert len(r.marks) > 250


def test_route_unknown_file_is_flagged_not_parsed(workbook_path):
    # the ALIC workbook is not a broker statement -> unknown, hand to agent
    r = route(workbook_path)
    assert r.status == "unknown"
    assert r.marks == []


def test_each_file_matches_at_most_one_spec(bnp_statement_path, baml_statement_path,
                                            gs_statement_path, hsbc_statement_path):
    # anchors must be distinctive: no file should match two specs (ambiguous)
    for path in (bnp_statement_path, baml_statement_path, gs_statement_path, hsbc_statement_path):
        assert len(identify(path)) == 1
