"""Checkout flow."""

from __future__ import annotations

from decimal import Decimal

from shopfront.payments.client import PaymentClient

payments = PaymentClient()


def checkout(total: Decimal) -> str:
    return payments.charge(total)
