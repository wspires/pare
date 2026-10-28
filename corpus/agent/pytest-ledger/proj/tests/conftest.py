import logging

import pytest

from ledger.accounts import Ledger

log = logging.getLogger("tests.fixtures")


@pytest.fixture
def ledger():
    lg = Ledger()
    for name, kind in [("cash", "asset"), ("bank", "asset"), ("card", "liability"),
                       ("salary", "income"), ("rent", "expense"), ("food", "expense"),
                       ("opening", "equity")]:
        lg.open(name, kind)
    log.info("fixture ledger with %d accounts", len(lg.kinds))
    yield lg
    log.info("fixture ledger torn down after %d transactions", len(lg.transactions))
