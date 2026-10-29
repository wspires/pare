import logging

import pytest

from ledger.money import format_amount, parse_amount, split_evenly

log = logging.getLogger(__name__)


@pytest.mark.parametrize("text,cents", [
    ("0", 0), ("1", 100), ("12.34", 1234), ("0.5", 50), ("0.05", 5), ("-5", -500),
    ("-0.01", -1), (" 7.70 ", 770), ("100.00", 10000), ("99.99", 9999),
    ("3.1", 310), ("-12.34", -1234), ("1000000", 100000000), ("0.00", 0), ("42", 4200),
    ("0.10", 10), ("0.01", 1), ("-100", -10000), ("8.08", 808), ("64.00", 6400),
])
def test_parse_amount(text, cents):
    log.info("parsing %r", text)
    assert parse_amount(text) == cents


@pytest.mark.parametrize("bad", ["", "abc", "1.234", "1,00", "--1", "$5", "5-", "1e3"])
def test_parse_amount_rejects(bad):
    with pytest.raises(ValueError):
        parse_amount(bad)


@pytest.mark.parametrize("cents,text", [
    (0, "0.00"), (100, "1.00"), (1234, "12.34"), (50, "0.50"), (5, "0.05"), (-500, "-5.00"),
    (-1, "-0.01"), (770, "7.70"), (10000, "100.00"), (9999, "99.99"), (-1234, "-12.34"),
    (1, "0.01"), (10, "0.10"), (808, "8.08"), (6400, "64.00"), (123456789, "1234567.89"),
])
def test_format_amount(cents, text):
    log.info("formatting %d", cents)
    assert format_amount(cents) == text


@pytest.mark.parametrize("text", ["12.34", "0.05", "-5.00", "100.00", "0.00", "-0.01", "7.70"])
def test_roundtrip(text):
    assert format_amount(parse_amount(text)) == text


@pytest.mark.parametrize("cents,ways", [(100, 3), (10, 4), (7, 7), (1, 2), (999, 10), (0, 3), (-100, 3), (-7, 2), (12345, 6)])
def test_split_sums(cents, ways):
    parts = split_evenly(cents, ways)
    log.info("parts %s", parts)
    assert sum(parts) == cents
    assert len(parts) == ways
    assert max(parts) - min(parts) <= 1


def test_split_rejects_zero_ways():
    with pytest.raises(ValueError):
        split_evenly(100, 0)
