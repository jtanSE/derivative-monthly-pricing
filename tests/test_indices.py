from derivative_pricing.reference.indices import canonicalize_index


def test_case_variants_collapse():
    assert canonicalize_index("S&P DYNAMIC INTRADAY TCA") == "S&P Dynamic Intraday TCA"
    assert canonicalize_index("S&P Dynamic Intraday TCA") == "S&P Dynamic Intraday TCA"


def test_whitespace_cleanup():
    assert canonicalize_index("  S&P 500 ") == "S&P 500"


def test_unknown_names_pass_through_unchanged():
    assert canonicalize_index("Some Future Index 7") == "Some Future Index 7"


def test_barclays_placeholder_not_silently_renamed():
    assert canonicalize_index("Barclays") == "Barclays"