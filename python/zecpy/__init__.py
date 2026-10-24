"""Shielded Zcash for Python, built on librustzcash."""

from ._native import Balance, NetworkError, PrivacyError, WalletError, ZecPyError
from .invoices import Invoice, Invoices, InvoiceStatus, PaymentEvent
from .wallet import Mnemonic, Payout, Wallet

__all__ = [
    "Balance",
    "ColdSigner",
    "Invoice",
    "InvoiceStatus",
    "Invoices",
    "Mnemonic",
    "NetworkError",
    "PaymentEvent",
    "Payout",
    "PrivacyError",
    "Wallet",
    "WalletError",
    "WatchBundle",
    "ZecPyError",
]


def __getattr__(name: str) -> object:
    # Loaded on first use, so `python -m zecpy.cold` does not import the module twice.
    if name in ("ColdSigner", "WatchBundle"):
        from . import cold

        return getattr(cold, name)
    raise AttributeError(f"module 'zecpy' has no attribute {name!r}")
