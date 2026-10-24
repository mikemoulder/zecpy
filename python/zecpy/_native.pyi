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
        self, to: str, amount_zat: int, memo: str | None = None, allow_transparent: bool = False
    ) -> bytes: ...
    def broadcast_signed(self, signed: bytes, max_broadcast_delay: float = 0.0) -> Awaitable[str]: ...
    def sync(self) -> Awaitable[int]: ...
    def balance(self) -> Balance: ...
    def address(self) -> str: ...
    def address_at(self, index: int) -> str: ...
    def received(self, since: int = 0) -> list[ReceivedOutput]: ...
    @property
    def network(self) -> str: ...
