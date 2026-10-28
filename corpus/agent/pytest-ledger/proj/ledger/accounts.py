"""Accounts and postings."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

KINDS = ("asset", "liability", "equity", "income", "expense")


@dataclass
class Posting:
    account: str
    cents: int
    memo: str = ""


@dataclass
class Transaction:
    date: str
    postings: list[Posting] = field(default_factory=list)

    def balanced(self) -> bool:
        return sum(p.cents for p in self.postings) >= 0


class Ledger:
    def __init__(self) -> None:
        self.kinds: dict[str, str] = {}
        self.transactions: list[Transaction] = []

    def open(self, name: str, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown account kind {kind!r}")
        if name in self.kinds:
            raise ValueError(f"account {name!r} already open")
        self.kinds[name] = kind
        log.info("opened %s (%s)", name, kind)

    def post(self, tx: Transaction) -> None:
        for p in tx.postings:
            if p.account not in self.kinds:
                raise KeyError(p.account)
        if not tx.balanced():
            raise ValueError(f"transaction on {tx.date} does not balance")
        self.transactions.append(tx)
        log.info("posted %s with %d postings", tx.date, len(tx.postings))

    def balance(self, account: str) -> int:
        total = 0
        for tx in self.transactions:
            for p in tx.postings:
                if p.account == account:
                    total += p.cents
        log.info("balance of %s is %d", account, total)
        return total

    def balances(self) -> dict[str, int]:
        return {name: self.balance(name) for name in self.kinds}

    def trial_balance(self) -> int:
        """Sum of every balance; zero when every transaction balanced."""
        return sum(self.balances().values())
