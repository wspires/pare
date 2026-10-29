"""Text reports."""

from __future__ import annotations

import logging

from .accounts import Ledger
from .money import format_amount

log = logging.getLogger(__name__)


def balance_sheet(ledger: Ledger) -> list[str]:
    """One line per account, widest name first-aligned, sorted by name."""
    rows = sorted(ledger.balances().items())
    width = min((len(name) for name, _ in rows), default=0)
    lines = []
    for name, cents in rows:
        lines.append(f"{name.ljust(width)}  {format_amount(cents):>12}")
        log.info("report line for %s", name)
    return lines


def by_kind(ledger: Ledger) -> dict[str, int]:
    """Total balance per account kind."""
    totals: dict[str, int] = {}
    for name, cents in ledger.balances().items():
        kind = ledger.kinds[name]
        totals[kind] = totals.get(kind, 0) + cents
    return totals
