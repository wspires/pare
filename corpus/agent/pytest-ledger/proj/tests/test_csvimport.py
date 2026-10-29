import logging

import pytest

from ledger.csvimport import import_csv, read_rows

log = logging.getLogger(__name__)

CSV = """date,account,amount,memo
2026-04-01,bank,2500.00,salary april
2026-04-01,salary,-2500.00,salary april
2026-04-02,rent,-1200.00,april rent
2026-04-02,bank,1200.00,april rent
2026-04-03,food,45.50,groceries
2026-04-03,card,-45.50,groceries
"""


def test_read_rows_count():
    assert len(read_rows(CSV)) == 6


def test_read_rows_fields():
    row = read_rows(CSV)[0]
    log.info("first row %s", row)
    assert row == {"date": "2026-04-01", "account": "bank", "amount": "2500.00", "memo": "salary april"}


def test_import_counts_transactions(ledger):
    assert import_csv(ledger, CSV) == 3


def test_import_balances(ledger):
    import_csv(ledger, CSV)
    assert ledger.balance("bank") == 370000
    assert ledger.balance("food") == 4550


@pytest.mark.parametrize("n", [1, 2, 5, 10, 25, 50])
def test_import_many_dates(ledger, n):
    lines = ["date,account,amount,memo"]
    for i in range(n):
        lines.append(f"2026-05-{i + 1:02d},cash,1.00,day {i}")
        lines.append(f"2026-05-{i + 1:02d},opening,-1.00,day {i}")
    log.info("importing %d dates", n)
    assert import_csv(ledger, "\n".join(lines) + "\n") == n
    assert ledger.balance("cash") == 100 * n


def test_import_unbalanced_day_fails(ledger):
    bad = "date,account,amount,memo\n2026-06-01,cash,1.00,\n2026-06-01,opening,-2.00,\n"
    with pytest.raises(ValueError):
        import_csv(ledger, bad)


def test_import_missing_memo_column(ledger):
    text = "date,account,amount\n2026-07-01,cash,1.00\n2026-07-01,opening,-1.00\n"
    assert import_csv(ledger, text) == 1
    assert ledger.transactions[0].postings[0].memo == ""
