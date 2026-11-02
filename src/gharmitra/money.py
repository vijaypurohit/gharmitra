"""Money in paise and en-IN formatting (₹1,200, ₹1,20,000)."""

from __future__ import annotations


def rupees(amount: int | float) -> int:
    """Rupees to paise."""
    return round(amount * 100)


def format_inr(paise: int) -> str:
    """Indian digit grouping: last three digits, then pairs. Paise shown only when non-zero."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join([head, *pairs, tail])
    cents = f".{frac:02d}" if frac else ""
    return f"{sign}₹{digits}{cents}"
