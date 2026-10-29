import logging

import pytest

from ledger.accounts import Posting, Transaction
from ledger.report import balance_sheet, by_kind

log = logging.getLogger(__name__)


@pytest.fixture
def filled(ledger):
    ledger.post(Transaction("2026-03-01", [Posting("bank", 250000), Posting("salary", -250000)]))
    ledger.post(Transaction("2026-03-02", [Posting("rent", 120000), Posting("bank", -120000)]))
    ledger.post(Transaction("2026-03-03", [Posting("food", 4550), Posting("card", -4550)]))
    ledger.post(Transaction("2026-03-04", [Posting("cash", 10000), Posting("bank", -10000)]))
    log.info("filled ledger with %d transactions", len(ledger.transactions))
    return ledger


def test_balance_sheet_has_a_line_per_account(filled):
    lines = balance_sheet(filled)
    assert len(lines) == len(filled.kinds)


def test_balance_sheet_is_sorted(filled):
    names = [line.split()[0] for line in balance_sheet(filled)]
    assert names == sorted(names)


def test_balance_sheet_amounts(filled):
    lines = dict(line.split(None, 1) for line in balance_sheet(filled))
    assert lines["bank"].strip() == "1200.00"
    assert lines["rent"].strip() == "1200.00"
    assert lines["card"].strip() == "-45.50"


def test_balance_sheet_aligns_names(filled):
    lines = balance_sheet(filled)
    lengths = {len(line) for line in lines}
    log.info("line lengths %s", lengths)
    assert len(lengths) == 1


@pytest.mark.parametrize("kind,total", [
    ("asset", 130000), ("liability", -4550), ("income", -250000), ("expense", 124550), ("equity", 0),
])
def test_by_kind(filled, kind, total):
    assert by_kind(filled)[kind] == total


def test_by_kind_sums_to_zero(filled):
    assert sum(by_kind(filled).values()) == 0


def test_empty_ledger_report(ledger):
    assert len(balance_sheet(ledger)) == len(ledger.kinds)
    assert all(line.endswith("0.00") for line in balance_sheet(ledger))
