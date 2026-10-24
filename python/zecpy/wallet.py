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
from typing import TYPE_CHECKING, Iterable, Mapping

from ._native import Balance, MemoReport, NativeWallet, PaymentRequest, Proposal, Transaction
from .invoices import Invoices

if TYPE_CHECKING:
    from .cold import WatchBundle

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
        self._invoices: Invoices | None = None

    @property
    def invoices(self) -> Invoices:
        """Invoices with their own shielded addresses, and exactly-once payment events."""
        if self._invoices is None:
            self._invoices = Invoices(self)
        return self._invoices

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
    async def watch(
        cls,
        path: str | os.PathLike[str],
        bundle: "WatchBundle | str",
        *,
        server: str,
        birthday: int | None = None,
        tor: bool = True,
        regtest_heights: Mapping[str, int] | None = None,
    ) -> Wallet:
        """Creates a watch-only wallet from a cold signer's watch bundle.

        It syncs, reports balances, issues invoices and builds unsigned transactions with
        ``propose_cold``, but cannot spend: the key that can lives on the cold signer.
        """
        from .cold import WatchBundle

        if isinstance(bundle, str):
            bundle = WatchBundle.from_json(bundle)
        wallet_dir = _prepare_new(path)
        heights = dict(regtest_heights) if regtest_heights is not None else None
        native = await NativeWallet.watch(
            str(wallet_dir / _DB), bundle.network, heights, server, tor,
            bundle.ufvk, bundle.seed_fingerprint, bundle.account, birthday,
        )
        _Config(bundle.network, heights).save(wallet_dir)
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

    async def fetch_memos(self, *, decoys: int = 7, max_delay: float = 10.0) -> MemoReport:
        """Fetches the memos of received payments. Opt-in: nothing calls it for you.

        Compact blocks do not carry memos, so each memo needs its full transaction, requested
        by txid. ZecPy never requests a txid alone: each real request is shuffled among
        ``decoys`` nearby shielded transactions, placed so the real one's rank in the set is
        uniform, on a Tor circuit used for nothing else, after a random delay of up to
        ``max_delay`` seconds. Raises ``PrivacyError`` without Tor unless the server runs on
        this machine. A payment waits until enough blocks have been scanned to hide it.

        Invoices do not need this: they match payments by address, not by memo.
        """
        return await self._native.fetch_memos(decoys, max_delay)

    def balance(self) -> Balance:
        """Balances in zatoshis, split by pool and by spendability."""
        return self._native.balance()

    def address(self) -> str:
        """A fresh Unified Address with shielded receivers only."""
        return self._native.address()

    def transactions(self, since: int = 0) -> list[Transaction]:
        """The wallet's transactions first seen at or after height ``since``, newest first.

        Read from the local database only: listing history sends nothing to the server.
        Memos the wallet sent are included; outputs hide memo contents from their repr.
        """
        return self._native.transactions(since)

    def transaction(self, txid: str) -> Transaction | None:
        """One transaction by txid, or None if the wallet has no record of it. Local only."""
        return self._native.transaction(txid)

    async def send(
        self,
        to: str,
        amount_zat: int,
        memo: str | None = None,
        *,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
        max_broadcast_delay: float = 0.0,
    ) -> str:
        """Pays ``amount_zat`` zatoshis to ``to`` and returns the txid.

        Spends shielded funds only. A transparent-only recipient raises ``PrivacyError``
        unless ``allow_transparent`` is set, because the payment would be public on chain.
        The transaction is proved and signed locally; only the finished transaction is sent
        to the server, over a Tor circuit used for nothing else.

        ``max_broadcast_delay`` holds the finished transaction for a random time up to that
        many seconds before publishing it, so it does not appear on the network at the
        moment of the action that caused it (a checkout, a payroll run).

        A payment that would move value between shielded pools (Orchard notes paying an
        Ironwood receiver) publishes the crossing amount, so it raises ``PrivacyError``
        unless ``allow_pool_crossing`` is set. ZIP 318 canonical migrations are allowed.
        """
        return await self._native.send(
            to, amount_zat, memo, allow_transparent, max_broadcast_delay, allow_pool_crossing
        )

    async def send_many(
        self,
        payments: "Iterable[Payout | tuple[str, int] | tuple[str, int, str | None]]",
        *,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
        max_broadcast_delay: float = 0.0,
    ) -> str:
        """Pays up to 50 recipients in one transaction and returns its txid.

        Cheaper than separate sends, and an observer sees one shielded transaction instead
        of several sent together. Every payment is validated before anything is built; one
        bad entry rejects the batch and names its position.
        """
        return await self._native.send_many(
            _payouts(payments), allow_transparent, max_broadcast_delay, allow_pool_crossing
        )

    def parse_request(self, uri: str) -> PaymentRequest:
        """Parses a ZIP-321 payment request (``zcash:...``) for display before paying it.

        Labels and messages are written by whoever made the link: show them, never act on
        them. Unknown required (``req-``) parameters make the request invalid, as ZIP-321
        demands. Local only.
        """
        return self._native.parse_request(uri)

    async def pay(
        self,
        uri: str,
        *,
        max_zat: int,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
        max_broadcast_delay: float = 0.0,
    ) -> str:
        """Pays a ZIP-321 payment request and returns the txid.

        ``max_zat`` is required: a request asking for more in total raises ``PrivacyError``
        before anything is built, so a bot or agent paying links it was handed cannot be
        talked into an amount it did not budget for. A request without an amount is refused
        rather than guessed. Every payment gets the same checks as ``send``.
        """
        return await self._native.pay(
            uri, max_zat, allow_transparent, allow_pool_crossing, max_broadcast_delay
        )

    def propose(
        self,
        to: str,
        amount_zat: int,
        memo: str | None = None,
        *,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> Proposal:
        """Plans a payment without building it: fee, notes spent, change, and any value it
        would make public by crossing pools. Local only. Send it with ``send_proposal``.

        The same refusals as ``send`` apply here, before anything is shown.
        """
        return self._native.propose([(to, amount_zat, memo)], allow_transparent, allow_pool_crossing)

    def propose_many(
        self,
        payments: "Iterable[Payout | tuple[str, int] | tuple[str, int, str | None]]",
        *,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> Proposal:
        """``propose`` for up to 50 recipients in one transaction."""
        return self._native.propose(_payouts(payments), allow_transparent, allow_pool_crossing)

    def propose_sweep(
        self,
        to: str,
        memo: str | None = None,
        *,
        mode: str = "spendable",
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> Proposal:
        """Plans sending everything to ``to``, the fee taken out of it. Local only.

        ``mode="spendable"`` sends what is spendable now and leaves notes still waiting for
        confirmations. ``mode="everything"`` raises ``WalletError`` instead of leaving
        anything behind, for when the wallet must really end up empty. Spends Orchard and
        Ironwood notes; Sapling notes, which ZecPy only receives, stay where they are.
        """
        if mode not in ("spendable", "everything"):
            raise ValueError('mode must be "spendable" or "everything"')
        return self._native.propose_sweep(
            to, memo, mode == "everything", allow_transparent, allow_pool_crossing
        )

    async def send_proposal(self, proposal: Proposal, *, max_broadcast_delay: float = 0.0) -> str:
        """Builds, signs and broadcasts exactly the transaction ``proposal`` describes, so
        the fee paid is the fee shown. Raises ``WalletError``, building nothing, if a note
        it spends was spent or rolled back since it was proposed; propose again."""
        return await self._native.send_proposal(proposal, max_broadcast_delay)

    def propose_cold_many(
        self,
        payments: "Iterable[Payout | tuple[str, int] | tuple[str, int, str | None]]",
        *,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> bytes:
        """``send_many`` as an unsigned PCZT for a cold signer."""
        return bytes(
            self._native.propose_cold_many(_payouts(payments), allow_transparent, allow_pool_crossing)
        )

    def propose_cold(
        self,
        to: str,
        amount_zat: int,
        memo: str | None = None,
        *,
        allow_transparent: bool = False,
        allow_pool_crossing: bool = False,
    ) -> bytes:
        """Builds and proves an unsigned transaction (a PCZT) for a cold signer.

        Works on watch-only wallets: Orchard and Ironwood proofs need no secret. The same
        privacy rules as ``send`` apply.
        """
        return bytes(
            self._native.propose_cold(to, amount_zat, memo, allow_transparent, allow_pool_crossing)
        )

    async def broadcast_signed(self, signed_pczt: bytes, *, max_broadcast_delay: float = 0.0) -> str:
        """Publishes a transaction the cold signer signed, on a Tor circuit of its own."""
        return await self._native.broadcast_signed(signed_pczt, max_broadcast_delay)

    def __repr__(self) -> str:
        return f"Wallet(path={str(self.path)!r}, network={self.network!r})"


@dataclass(frozen=True)
class Payout:
    """One recipient of a batch payment."""

    address: str
    amount_zat: int
    memo: str | None = None


def _payouts(payments: "Iterable[Payout | tuple]") -> list[tuple[str, int, str | None]]:
    out = []
    for p in payments:
        if isinstance(p, Payout):
            out.append((p.address, p.amount_zat, p.memo))
        else:
            address, amount, *rest = p
            out.append((address, amount, rest[0] if rest else None))
    return out


def _prepare_new(path: str | os.PathLike[str]) -> Path:
    wallet_dir = Path(path)
    if (wallet_dir / _DB).exists():
        raise FileExistsError(f"a wallet already exists at {wallet_dir}")
    wallet_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(wallet_dir, 0o700)
    return wallet_dir
