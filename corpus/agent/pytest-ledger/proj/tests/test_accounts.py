import logging

import pytest

from ledger.accounts import KINDS, Ledger, Posting, Transaction

log = logging.getLogger(__name__)


@pytest.mark.parametrize("kind", KINDS)
def test_open_each_kind(kind):
    lg = Ledger()
    lg.open("a", kind)
    assert lg.kinds["a"] == kind


def test_open_unknown_kind():
    with pytest.raises(ValueError):
        Ledger().open("a", "wealth")


def test_open_twice(ledger):
    with pytest.raises(ValueError):
        ledger.open("cash", "asset")


@pytest.mark.parametrize("n", range(1, 13))
def test_post_and_balance(ledger, n):
    for i in range(n):
        ledger.post(Transaction(f"2026-01-{i + 1:02d}", [Posting("cash", 1000), Posting("salary", -1000)]))
    log.info("posted %d salary transactions", n)
    assert ledger.balance("cash") == 1000 * n
    assert ledger.balance("salary") == -1000 * n
    assert ledger.trial_balance() == 0


def test_post_unbalanced(ledger):
    with pytest.raises(ValueError):
        ledger.post(Transaction("2026-01-01", [Posting("cash", 1000), Posting("salary", -900)]))


def test_post_unknown_account(ledger):
    with pytest.raises(KeyError):
        ledger.post(Transaction("2026-01-01", [Posting("cash", 1), Posting("nowhere", -1)]))


@pytest.mark.parametrize("amount", [1, 250, 999, 12345, 100000])
def test_three_way_transaction(ledger, amount):
    ledger.post(Transaction("2026-02-01", [Posting("rent", amount), Posting("food", amount), Posting("bank", -2 * amount)]))
    log.info("balances %s", ledger.balances())
    assert ledger.balances()["bank"] == -2 * amount
    assert ledger.trial_balance() == 0


def test_balances_lists_every_account(ledger):
    assert set(ledger.balances()) == set(ledger.kinds)


def test_balance_of_untouched_account_is_zero(ledger):
    assert ledger.balance("opening") == 0
