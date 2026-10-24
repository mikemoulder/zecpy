"""The developer-facing wallet.

A wallet is a directory holding the zcash_client_sqlite database (``wallet.db``) and a small
``zecpy.json`` recording which network it belongs to. The seed is never written to disk:
callers receive the mnemonic once, at creation, and are responsible for storing it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ._native import Balance, NativeWallet

_DB = "wallet.db"
_CONFIG = "zecpy.json"


class Mnemonic:
    """A BIP-39 phrase that does not print itself by accident."""

    __slots__ = ("_phrase",)

    def __init__(self, phrase: str) -> None:
        self._phrase = phrase

    def reveal(self) -> str:
        """Returns the phrase. Store it offline; it controls the funds."""
        return self._phrase

    def __repr__(self) -> str:
        return "Mnemonic(<24 words, hidden>)"

    __str__ = __repr__


@dataclass(frozen=True)
class _Config:
    network: str
    heights: dict[str, int] | None

    @classmethod
    def load(cls, wallet_dir: Path) -> _Config:
        data = json.loads((wallet_dir / _CONFIG).read_text())
        return cls(network=data["network"], heights=data.get("heights"))

    def save(self, wallet_dir: Path) -> None:
        path = wallet_dir / _CONFIG
        path.write_text(json.dumps({"network": self.network, "heights": self.heights}, indent=2))


class Wallet:
    """A single-account shielded wallet."""

    def __init__(self, native: NativeWallet, path: Path) -> None:
        self._native = native
        self.path = path

    @classmethod
    async def create(
        cls,
        path: str | os.PathLike[str],
        *,
        network: str,
        server: str,
        tor: bool = True,
        regtest_heights: Mapping[str, int] | None = None,
    ) -> tuple[Wallet, Mnemonic]:
        """Creates a new wallet with a fresh 24-word seed.

        Returns the wallet and its mnemonic. The mnemonic is not stored anywhere; losing it
        means losing the funds if the wallet directory is lost.
        """
        wallet_dir = _prepare_new(path)
        heights = dict(regtest_heights) if regtest_heights is not None else None
        native, phrase, _birthday = await NativeWallet.create(
            str(wallet_dir / _DB), network, heights, server, tor
        )
        _Config(network, heights).save(wallet_dir)
        return cls(native, wallet_dir), Mnemonic(phrase)

    @classmethod
    async def restore(
        cls,
        path: str | os.PathLike[str],
        mnemonic: Mnemonic | str,
        *,
        network: str,
        server: str,
        birthday: int | None = None,
        tor: bool = True,
        regtest_heights: Mapping[str, int] | None = None,
    ) -> Wallet:
        """Recreates a wallet from its mnemonic. Without a birthday it scans the whole chain."""
        wallet_dir = _prepare_new(path)
        phrase = mnemonic.reveal() if isinstance(mnemonic, Mnemonic) else mnemonic
        heights = dict(regtest_heights) if regtest_heights is not None else None
        native, _phrase, _birthday = await NativeWallet.create(
            str(wallet_dir / _DB), network, heights, server, tor, phrase, birthday
        )
        _Config(network, heights).save(wallet_dir)
        return cls(native, wallet_dir)

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        server: str,
        mnemonic: Mnemonic | str | None = None,
        tor: bool = True,
    ) -> Wallet:
        """Opens an existing wallet directory.

        Without the mnemonic the wallet is watch-only: it syncs, reports balances and derives
        addresses, but cannot send. The mnemonic is checked against the wallet's keys.
        """
        wallet_dir = Path(path)
        config = _Config.load(wallet_dir)
        phrase = mnemonic.reveal() if isinstance(mnemonic, Mnemonic) else mnemonic
        native = NativeWallet.open(
            str(wallet_dir / _DB), config.network, config.heights, server, tor, phrase
        )
        return cls(native, wallet_dir)

    @property
    def network(self) -> str:
        return self._native.network

    @property
    def can_send(self) -> bool:
        """False for watch-only wallets."""
        return self._native.can_send

    async def sync(self) -> int:
        """Scans the chain up to its tip. Returns the height the wallet is synced to."""
        return await self._native.sync()

    def balance(self) -> Balance:
        """Balances in zatoshis, split by pool and by spendability."""
        return self._native.balance()

    def address(self) -> str:
        """A fresh Unified Address with shielded receivers only."""
        return self._native.address()

    async def send(
        self,
        to: str,
        amount_zat: int,
        memo: str | None = None,
        *,
        allow_transparent: bool = False,
    ) -> str:
        """Pays ``amount_zat`` zatoshis to ``to`` and returns the txid.

        Spends shielded funds only. A transparent-only recipient raises ``PrivacyError``
        unless ``allow_transparent`` is set, because the payment would be public on chain.
        The transaction is proved and signed locally; only the finished transaction is sent
        to the server.
        """
        return await self._native.send(to, amount_zat, memo, allow_transparent)

    def __repr__(self) -> str:
        return f"Wallet(path={str(self.path)!r}, network={self.network!r})"


def _prepare_new(path: str | os.PathLike[str]) -> Path:
    wallet_dir = Path(path)
    if (wallet_dir / _DB).exists():
        raise FileExistsError(f"a wallet already exists at {wallet_dir}")
    wallet_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(wallet_dir, 0o700)
    return wallet_dir
