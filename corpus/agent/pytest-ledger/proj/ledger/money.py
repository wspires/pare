"""Amounts in minor units (cents), so there is no floating point anywhere."""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_AMOUNT = re.compile(r"^(-)?(\d+)(?:\.(\d{1,2}))?$")


def parse_amount(text: str) -> int:
    """'12.34' -> 1234, '-5' -> -500, '0.5' -> 50."""
    m = _AMOUNT.match(text.strip())
    if not m:
        raise ValueError(f"not an amount: {text!r}")
    sign, whole, frac = m.groups()
    frac = (frac or "").rjust(2, "0")
    cents = int(whole) * 100 + int(frac)
    log.info("parsed %r as %d cents", text, cents)
    return -cents if sign else cents


def format_amount(cents: int) -> str:
    """1234 -> '12.34', -5 -> '-0.05'."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def split_evenly(cents: int, ways: int) -> list[int]:
    """Split an amount into `ways` parts that sum to it exactly; the first
    parts carry the remainder."""
    if ways <= 0:
        raise ValueError("ways must be positive")
    base, rem = divmod(abs(cents), ways)
    parts = [base + (1 if i < rem else 0) for i in range(ways)]
    if cents < 0:
        parts = [-p for p in parts]
    log.info("split %d into %d parts: %s", cents, ways, parts)
    return parts
