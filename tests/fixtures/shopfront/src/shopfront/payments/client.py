"""Payment gateway client. This is the supported way to charge and refund."""

from __future__ import annotations

from decimal import Decimal


class PaymentClient:
    def charge(self, amount: Decimal) -> str:
        return f"charge:{amount}"

    def refund(self, amount: Decimal) -> str:
        return f"refund:{amount}"
