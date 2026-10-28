"""Import postings from a CSV of `date,account,amount,memo` lines, one
transaction per date."""

from __future__ import annotations

import csv
import io
import logging

from .accounts import Ledger, Posting, Transaction
from .money import parse_amount

log = logging.getLogger(__name__)


def read_rows(text: str) -> list[dict[str, str]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    log.info("read %d csv rows", len(rows))
    return rows


def import_csv(ledger: Ledger, text: str) -> int:
    """Post one transaction per date; returns how many were posted."""
    by_date: dict[str, Transaction] = {}
    for row in read_rows(text):
        tx = by_date.setdefault(row["date"], Transaction(row["date"]))
        tx.postings.append(Posting(row["account"], parse_amount(row["amount"]), row["memo"]))
    for date in sorted(by_date):
        ledger.post(by_date[date])
    return len(by_date)
