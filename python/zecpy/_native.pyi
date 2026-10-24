from typing import Awaitable

class ZecPyError(Exception): ...
class NetworkError(ZecPyError): ...
class WalletError(ZecPyError): ...
class PrivacyError(ZecPyError): ...

class Balance:
    total: int
    spendable: int
    pending: int
    sapling: int
    orchard: int
    ironwood: int
    chain_tip: int
    scanned_height: int

class ReceivedOutput:
    txid: str
    pool: str
    output_index: int
    value: int
    mined_height: int | None
    diversifier_index: int | None

class TxOutput:
    pool: str
    output_index: int
    direction: str
    value: int
    address: str | None
    diversifier_index: int | None
    memo: str | None
    memo_bytes: bytes | None

class Transaction:
    txid: str
    status: str
    mined_height: int | None
    confirmations: int
    block_time: int | None
    expiry_height: int | None
    net_value: int
    fee: int | None
    pool_crossing_value: int | None
    memo_count: int
    memo_pending: bool
    outputs: list[TxOutput]

class MemoReport:
    fetched: int
    waiting: int
    unavailable: int

class Proposal:
    fee: int
    paying_out: int
    payments: list[tuple[str, int]]
    spends: list[tuple[str, int]]
    change: list[tuple[str, int]]
    crossing_value: int
    canonical_crossing: bool
    target_height: int

class PaymentRequest:
    payments: list[tuple[str, int, str | None, str | None, bool]]
    total: int

class SignedOutput:
    pool: str
    value: int
    label: str | None
    is_change: bool

class Summary:
    spending: int
    outputs: list[SignedOutput]
    paying_out: int
    fee: int
    foreign_spends: int

class ColdSigner:
    def __init__(
        self, phrase: str, network: str, heights: dict[str, int] | None = None, account: int = 0
    ) -> None: ...
    @staticmethod
    def generate_mnemonic() -> str: ...
    def watch_bundle(self) -> dict[str, str]: ...
    def inspect(self, pczt: bytes) -> Summary: ...
    def sign(self, pczt: bytes) -> bytes: ...
    @property
    def network(self) -> str: ...

class NativeWallet:
    @staticmethod
    def create(
        path: str,
        network: str,
        heights: dict[str, int] | None,
        server: str,
        tor: bool,
        phrase: str | None = None,
        birthday: int | None = None,
    ) -> Awaitable[tuple[NativeWallet, str, int]]: ...
    @staticmethod
    def open(
        path: str,
        network: str,
        heights: dict[str, int] | None,
        server: str,
        tor: bool,
        phrase: str | None = None,
    ) -> NativeWallet: ...
    def send(
        self,
        to: str,
        amount_zat: int,
        memo: str | None = None,
        allow_transparent: bool = False,
        max_broadcast_delay: float = 0.0,
        allow_pool_crossing: bool = False,
    ) -> Awaitable[str]: ...
    @property
    def can_send(self) -> bool: ...
    @staticmethod
    def watch(
        path: str,
        network: str,
        heights: dict[str, int] | None,
        server: str,
        tor: bool,
        ufvk: str,
        seed_fingerprint: str,
        account: int,
        birthday: int | None = None,
    ) -> Awaitable[NativeWallet]: ...
    def propose_cold(
        self,
        to: str,
        amount_zat: int,
        memo: str | None = None,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> bytes: ...
    def propose_cold_many(
        self,
        payments: list[tuple[str, int, str | None]],
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> bytes: ...
    def propose(
        self,
        payments: list[tuple[str, int, str | None]],
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> Proposal: ...
    def send_proposal(self, proposal: Proposal, max_broadcast_delay: float = 0.0) -> Awaitable[str]: ...
    def propose_sweep(
        self,
        to: str,
        memo: str | None = None,
        everything: bool = False,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> Proposal: ...
    def parse_request(self, uri: str) -> PaymentRequest: ...
    def pay(
        self,
        uri: str,
        max_zat: int,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
        max_broadcast_delay: float = 0.0,
    ) -> Awaitable[str]: ...
    def send_many(
        self,
        payments: list[tuple[str, int, str | None]],
        allow_transparent: bool = False,
        max_broadcast_delay: float = 0.0,
        allow_pool_crossing: bool = False,
    ) -> Awaitable[str]: ...
    def broadcast_signed(self, signed: bytes, max_broadcast_delay: float = 0.0) -> Awaitable[str]: ...
    def sync(self) -> Awaitable[int]: ...
    def fetch_memos(self, decoys: int = 7, max_delay: float = 10.0) -> Awaitable[MemoReport]: ...
    def balance(self) -> Balance: ...
    def address(self) -> str: ...
    def address_at(self, index: int) -> str: ...
    def received(self, since: int = 0) -> list[ReceivedOutput]: ...
    def transactions(self, since: int = 0) -> list[Transaction]: ...
    def transaction(self, txid: str) -> Transaction | None: ...
    @property
    def network(self) -> str: ...
