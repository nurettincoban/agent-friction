"""Old payment helper, kept until the last callers are migrated to PaymentClient."""

from __future__ import annotations

from decimal import Decimal


def legacy_charge(amount: Decimal) -> str:
    return f"legacy:{amount}"
