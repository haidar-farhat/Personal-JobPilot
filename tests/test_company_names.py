"""Company-name normalization — canonical keys for company matching."""

from utils.company_names import normalize_company_name


def test_strips_legal_suffixes_and_case():
    assert normalize_company_name("Chime Financial, Inc") == "chime"
    assert normalize_company_name("Chime Financial, Inc.") == "chime"
    assert normalize_company_name("Affirm") == "affirm"
    assert normalize_company_name("Affirm, Inc.") == "affirm"


def test_strips_punctuation_and_whitespace():
    assert normalize_company_name("  Plaid   Inc ") == "plaid"
    assert normalize_company_name("Intuit - Credit Karma") == "intuit credit karma"


def test_common_corporate_words_removed_only_as_suffix_tokens():
    # "financial" as a suffix token goes; embedded words stay intact
    assert normalize_company_name("Brex Financial") == "brex"
    assert normalize_company_name("Coinbase Global, Inc.") == "coinbase"
    # A company literally named a suffix word alone is preserved
    assert normalize_company_name("Financial") == "financial"


def test_empty_and_none_safe():
    assert normalize_company_name("") == ""
    assert normalize_company_name(None) == ""
