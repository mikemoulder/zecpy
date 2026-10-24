"""Leak audit: record and classify every request a wallet sends to its light server.

The proxy sits between a wallet and lightwalletd, forwards bytes unchanged, and decodes a
copy of each request. Because it runs outside the wallet, its log does not depend on the
wallet reporting honestly about itself.
"""

from .classify import Label, classify
from .proxy import AuditProxy, Record

__all__ = ["AuditProxy", "Label", "Record", "classify"]
