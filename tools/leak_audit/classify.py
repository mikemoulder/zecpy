"""What a light server can learn from each request.

    public       every wallet sends the same thing; the request says nothing about this one
    coarse       reveals something, but only within a deliberately wide window
    broadcast    publishes a transaction; unavoidable when sending, hidden behind Tor
    identifying  ties the request to this wallet's addresses, transactions or exact history

Unknown methods are identifying: the audit fails closed.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

# Tree-state requests at heights just below a multiple of this are ones every wallet crossing
# that height makes. Matches ZecPy's sync batch alignment (src/sync.rs BATCH), which also
# divides its birthday bucket.
ALIGNMENT = 1_000
# Mainnet and Testnet. A restore without a birthday starts here, like every other one.
_SAPLING_ACTIVATION = {419_200, 280_000}


class Label(str, Enum):
    PUBLIC = "public"
    COARSE = "coarse"
    BROADCAST = "broadcast"
    IDENTIFYING = "identifying"


_PUBLIC = {
    "GetLatestBlock": "chain tip, asked by every wallet",
    "GetLightdInfo": "server information",
    "GetBlockRange": "a range of compact blocks; every syncing wallet downloads them all",
    "GetBlockRangeNullifiers": "a range of compact blocks",
    "GetSubtreeRoots": "note commitment subtree roots, the same for every wallet",
    "GetLatestTreeState": "tree state at the tip",
    "GetMempoolStream": "the whole mempool",
    "Ping": "liveness check",
}

_IDENTIFYING = {
    "GetTransaction": "fetches one transaction by txid: the server learns it is yours",
    "GetBlock": "fetches one specific block: the server learns it interests you",
    "GetBlockNullifiers": "fetches one specific block",
    "GetTaddressTxids": "lists a transparent address's transactions: links the address to you",
    "GetTaddressTransactions": "lists a transparent address's transactions",
    "GetTaddressBalance": "queries transparent address balances",
    "GetTaddressBalanceStream": "queries transparent address balances",
    "GetAddressUtxos": "queries transparent address UTXOs: links the addresses to you",
    "GetAddressUtxosStream": "queries transparent address UTXOs",
    "GetMempoolTx": "sends the txids the wallet already knows",
}


def classify(method: str, request: dict[str, Any]) -> tuple[Label, str]:
    """Labels one request. `request` is the decoded message as a dict (proto field names)."""
    if method in _PUBLIC:
        return Label.PUBLIC, _PUBLIC[method]
    if method == "SendTransaction":
        return Label.BROADCAST, "publishes a transaction"
    if method == "GetTreeState":
        return _tree_state(request)
    if method in _IDENTIFYING:
        return Label.IDENTIFYING, _IDENTIFYING[method]
    return Label.IDENTIFYING, f"unknown method {method}: assumed identifying"


def _tree_state(request: dict[str, Any]) -> tuple[Label, str]:
    if request.get("hash"):
        return Label.IDENTIFYING, "tree state by block hash"
    height = int(request.get("height", 0))
    if height in _SAPLING_ACTIVATION:
        return Label.COARSE, f"tree state at Sapling activation {height}: every full restore asks this"
    if (height + 1) % ALIGNMENT == 0 or height <= 1:
        return Label.COARSE, f"tree state at {height}: a {ALIGNMENT}-block boundary every wallet crossing it asks for"
    return Label.IDENTIFYING, (
        f"tree state at {height}: an exact height tied to this wallet "
        "(its birthday, or where its last sync stopped)"
    )
