"""Synthetic month-to-month robustness & format-drift tests.

Two guarantees, exercised on generated statements that mimic how real broker
files vary (extra columns, reordering, letterhead preamble, jittered numbers):

  1. CONSISTENCY -- noise a broker adds between months (new columns, a different
     column order) must not change our output. Same refs, same field values,
     same field->header mapping.

  2. DRIFT DETECTION -- when a broker actually changes the statement (renames or
     drops a column the spec depends on), the system must SAY SO rather than
     silently emit wrong/blank values: hard for a required column (won't parse),
     flagged for an optional one (parses, but reported).

See ``synth_statements`` for the generator.
"""

import pytest

from derivative_pricing.adaptive.agent import SPECS_DIR, load_spec, validate_spec
from derivative_pricing.adaptive.drift import check_spec_drift, compare_layouts
from derivative_pricing.adaptive.engine import run_spec
from derivative_pricing.adaptive.router import identify, route
from derivative_pricing.run_month import run_month
from synth_statements import default_profile, generate_statement, make_spec

PROFILE = default_profile()
SPEC = make_spec(PROFILE)

_FIELDS = ("mtm_local", "strike", "quantity", "put_call", "direction", "underlying")


def _by_ref(marks):
    return {m.trade_ref: m for m in marks}


def _field(m, name):
    return m.mtm_local if name == "mtm_local" else (
        m.currency if name == "currency" else m.extra.get(name))


# --- 1. Consistency under noise -------------------------------------------


def test_noise_and_reordering_do_not_change_output(tmp_path):
    """Same seed, but one file is bare and the other has 8 random extra columns
    in a shuffled order. Parsed output must be byte-for-byte equal per ref."""
    clean, _ = generate_statement(PROFILE, tmp_path / "clean.xlsx", seed=7,
                                  n_rows=35, extra_cols=0, shuffle=False)
    noisy, _ = generate_statement(PROFILE, tmp_path / "noisy.xlsx", seed=7,
                                  n_rows=35, extra_cols=8, shuffle=True)
    a, b = _by_ref(run_spec(SPEC, clean)), _by_ref(run_spec(SPEC, noisy))

    assert set(a) == set(b) and len(a) == 35
    for ref in a:
        assert a[ref].mtm_local == b[ref].mtm_local
        assert a[ref].currency == b[ref].currency
        for f in _FIELDS[1:]:
            assert a[ref].extra.get(f) == b[ref].extra.get(f), f"{ref}:{f}"


def test_parsed_output_matches_generator_truth(tmp_path):
    """The parse reproduces exactly what the generator wrote (guards the
    generator and the engine together)."""
    path, truth = generate_statement(PROFILE, tmp_path / "s.xlsx", seed=3,
                                     n_rows=30, extra_cols=6, shuffle=True)
    for m in run_spec(SPEC, path):
        t = truth[m.trade_ref]
        assert m.mtm_local == pytest.approx(t["mtm_local"])
        assert m.currency == "USD"
        assert m.extra.get("strike") == pytest.approx(t["strike"])
        assert m.extra.get("quantity") == pytest.approx(t["quantity"])
        assert m.extra.get("put_call") == t["put_call"]
        assert m.extra.get("underlying") == t["underlying"]
        assert m.extra.get("expiry") == t["expiry"].isoformat()
        assert m.extra.get("direction") == t["direction"]


def test_output_consistent_across_months(tmp_path):
    """Three 'months' of the same format: different noise + jittered numbers,
    same trades. Ref set and field->header mapping (layout signature) stay
    identical; only the numbers move."""
    months = []
    for i, (seed, extra) in enumerate([(1, 0), (2, 5), (3, 11)]):
        p, _ = generate_statement(PROFILE, tmp_path / f"m{i}.xlsx", seed=seed,
                                  n_rows=32, extra_cols=extra, shuffle=(i > 0))
        months.append(p)

    parsed = [run_spec(SPEC, p) for p in months]
    ref_sets = [set(_by_ref(m)) for m in parsed]
    assert ref_sets[0] == ref_sets[1] == ref_sets[2]

    sigs = [check_spec_drift(SPEC, p).layout_signature for p in months]
    assert sigs[0] == sigs[1] == sigs[2], "layout signature drifted across clean months"

    # sanity: months are genuinely different data (numbers jitter)
    ref = next(iter(ref_sets[0]))
    mtms = {_by_ref(m)[ref].mtm_local for m in parsed}
    assert len(mtms) == 3, "months should carry different values"


def test_router_identifies_broker_despite_noise(tmp_path):
    noisy, _ = generate_statement(PROFILE, tmp_path / "n.xlsx", seed=5,
                                  n_rows=20, extra_cols=10, shuffle=True)
    matches = identify(noisy, specs=[SPEC])
    assert len(matches) == 1
    assert matches[0].broker.value == PROFILE.broker
    r = route(noisy, specs=[SPEC])
    assert r.status == "known" and len(r.marks) == 20


def test_clean_months_report_no_drift(tmp_path):
    """Noise between two clean months is NOT drift: compare_layouts is empty."""
    a, _ = generate_statement(PROFILE, tmp_path / "a.xlsx", seed=1, extra_cols=3)
    b, _ = generate_statement(PROFILE, tmp_path / "b.xlsx", seed=2, extra_cols=9,
                              shuffle=True)
    assert compare_layouts(check_spec_drift(SPEC, a), check_spec_drift(SPEC, b)) == []


# --- 2. Drift detection ----------------------------------------------------


def test_renamed_required_column_is_hard_drift(tmp_path):
    """A broker renames the MTM column -> the file must NOT parse silently; the
    drift check flags a missing REQUIRED field and run_spec/validate fail."""
    clean, _ = generate_statement(PROFILE, tmp_path / "clean.xlsx", seed=1)
    drifted, _ = generate_statement(PROFILE, tmp_path / "drift.xlsx", seed=1,
                                    extra_cols=4, shuffle=True,
                                    rename={"mtm_local": "Valuation Amount"})

    rep = check_spec_drift(SPEC, drifted)
    assert not rep.ok
    assert "mtm_local" in rep.missing_required

    with pytest.raises(ValueError, match="not located"):
        run_spec(SPEC, drifted)

    assert not validate_spec(SPEC, drifted).ok

    diffs = compare_layouts(check_spec_drift(SPEC, clean), rep)
    assert any("mtm_local" in d for d in diffs)


def test_dropped_optional_column_parses_but_is_flagged(tmp_path):
    """A broker drops the Strike column -> the file STILL parses (strike is
    optional), but the drift check reports it so the silent loss is visible."""
    clean, _ = generate_statement(PROFILE, tmp_path / "clean.xlsx", seed=1)
    dropped, _ = generate_statement(PROFILE, tmp_path / "drop.xlsx", seed=1,
                                    extra_cols=4, shuffle=True, drop={"strike"})

    rep = check_spec_drift(SPEC, dropped)
    assert rep.ok  # still parses
    assert "strike" in rep.missing_optional

    marks = run_spec(SPEC, dropped)
    assert marks and all(m.extra.get("strike") is None for m in marks)

    diffs = compare_layouts(check_spec_drift(SPEC, clean), rep)
    assert any("strike" in d for d in diffs)


def test_missing_anchor_is_reported_not_raised(tmp_path):
    """If the anchor header is gone entirely, check_spec_drift reports
    header_found=False instead of blowing up (it's the strongest drift signal)."""
    gone, _ = generate_statement(PROFILE, tmp_path / "g.xlsx", seed=1,
                                 rename={"trade_ref": "Deal Identifier"})
    rep = check_spec_drift(SPEC, gone)
    assert rep.header_found is False
    assert not rep.ok


# --- 3. Real specs & pipeline integration ----------------------------------


def test_real_spec_layout_intact(bnp_statement_path):
    """The shipped BNP spec still fully resolves against the real BNP file --
    the drift check must not cry wolf on a known-good pairing."""
    rep = check_spec_drift(load_spec(SPECS_DIR / "bnp.json"), bnp_statement_path)
    assert rep.ok
    assert not rep.missing_required and not rep.missing_optional


def test_run_month_clean_has_no_warnings(tmp_path):
    stmts = tmp_path / "in"
    stmts.mkdir()
    generate_statement(PROFILE, stmts / "wf.xlsx", seed=1, n_rows=25)
    summary = run_month(stmts, tmp_path / "out", as_of="2026-04-30", specs=[SPEC])
    assert summary.format_warnings == []
    assert summary.known and summary.output_xlsx


def test_run_month_flags_drifted_broker(tmp_path):
    stmts = tmp_path / "in"
    stmts.mkdir()
    generate_statement(PROFILE, stmts / "wf.xlsx", seed=1, n_rows=25,
                       drop={"strike"})
    summary = run_month(stmts, tmp_path / "out", as_of="2026-04-30", specs=[SPEC])
    assert summary.format_warnings, "drifted layout should raise a format warning"
    broker, detail = summary.format_warnings[0]
    assert broker == PROFILE.broker
    assert "strike" in detail


def test_onboarding_notes_drift_on_known_file(tmp_path, monkeypatch):
    """Re-dropping an already-known broker whose layout drifted: onboard_file
    still says 'already_known' but the message calls out the changed column."""
    from derivative_pricing.adaptive.agent import onboard_file

    drifted, _ = generate_statement(PROFILE, tmp_path / "d.xlsx", seed=1,
                                    drop={"strike"})
    monkeypatch.setattr("derivative_pricing.adaptive.router.identify",
                        lambda p: [SPEC])
    res = onboard_file(drifted)
    assert res.status == "already_known"
    assert "layout changed" in res.message and "strike" in res.message


# --- 4. Adopting a changed format (update_spec + forced re-onboard) ---------


def test_update_spec_overwrites_in_place_with_backup(tmp_path):
    """Confirming a changed format overwrites the spec but keeps the old one in
    a backups subdir that load_reference_specs never picks up."""
    from derivative_pricing.adaptive.agent import load_spec, save_spec, update_spec

    save_spec(SPEC, specs_dir=tmp_path)
    changed = SPEC.model_copy(update={"header_anchor": "Deal Identifier"})
    path, backup = update_spec(changed, specs_dir=tmp_path)

    assert load_spec(path).header_anchor == "Deal Identifier"
    assert backup is not None and backup.exists()
    assert "Trade Reference" in backup.read_text(encoding="utf-8")  # old anchor preserved
    # only the live spec is a loadable *.json; the backup sits under backups/
    assert [p.name for p in tmp_path.glob("*.json")] == ["wells_fargo.json"]
    assert backup.parent.name == "backups"


def test_onboard_force_redrafts_a_known_broker(tmp_path, monkeypatch):
    """force=True re-drafts even though the broker is already known, so a
    changed format can be re-proposed (adopted later via update_spec)."""
    from derivative_pricing.adaptive import agent

    path, _ = generate_statement(PROFILE, tmp_path / "wf.xlsx", seed=1, n_rows=20)
    monkeypatch.setattr("derivative_pricing.adaptive.router.identify", lambda p: [SPEC])
    monkeypatch.setattr(agent, "drafting_backend", lambda: "api")
    monkeypatch.setattr(agent, "propose_spec", lambda p, model="claude-opus-4-8": SPEC)

    res = agent.onboard_file(path, force=True)
    assert res.status == "proposed"
    assert res.broker == PROFILE.broker


def test_reonboard_cycle_adopts_changed_format(tmp_path):
    """The full loop the UI drives: month 1 parses; month 2 renames the MTM
    column so the old spec fails; adopting the re-drafted spec via update_spec
    parses the new file and keeps the old layout as a backup."""
    from derivative_pricing.adaptive.agent import load_spec, save_spec, update_spec

    save_spec(SPEC, specs_dir=tmp_path)

    changed_file, _ = generate_statement(PROFILE, tmp_path / "m2.xlsx", seed=9,
                                         n_rows=20, extra_cols=4, shuffle=True,
                                         rename={"mtm_local": "Valuation Amount"})
    assert not check_spec_drift(SPEC, changed_file).ok  # old spec can't parse

    # the re-onboard proposal points mtm_local at the new header; adopt it
    new_spec = SPEC.model_copy(update={"columns": [
        c.model_copy(update={"header_contains": "Valuation Amount"})
        if c.field == "mtm_local" else c
        for c in SPEC.columns
    ]})
    _, backup = update_spec(new_spec, specs_dir=tmp_path)

    adopted = load_spec(tmp_path / "wells_fargo.json")
    assert check_spec_drift(adopted, changed_file).ok
    assert run_spec(adopted, changed_file)          # parses under the new layout
    assert backup is not None and backup.exists()   # old layout recoverable
