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
        self, to: str, amount_zat: int, memo: str | None = None, allow_transparent: bool = False
    ) -> Awaitable[str]: ...
    @property
    def can_send(self) -> bool: ...
    def sync(self) -> Awaitable[int]: ...
    def balance(self) -> Balance: ...
    def address(self) -> str: ...
    @property
    def network(self) -> str: ...
