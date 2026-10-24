"""Light Wallet Privacy Report: the same five scenarios, run by each wallet through the
audit proxy on the local regtest chain, with every request recorded and classified.

    python -m tools.leak_audit.report            # needs devnet/up.sh running

Writes docs/privacy-report.md and the raw request logs in docs/privacy-report/.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from tools.devnet import DEV, LWD, REPO, Devnet, activation_heights
from tools.leak_audit import AuditProxy, Label, Record

SCENARIOS = [
    ("create", "Create a new wallet"),
    ("first-sync", "First sync to the tip"),
    ("receive", "Receive a payment, sync"),
    ("restart", "Restart the process, sync"),
    ("send", "Send a payment"),
]
OUT_DIR = REPO / "docs" / "privacy-report"
PAYMENT = 1_000_000
SEND = 50_000


@dataclass
class Run:
    wallet: str
    version: str
    records: dict[str, list[Record]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Driver:
    """One wallet's way of performing the scenarios. Every network call it makes must go to
    `server` (the audit proxy)."""

    name: str
    version: str

    def __init__(self, workdir: Path, server: str, devnet: Devnet) -> None:
        self.workdir, self.server, self.devnet = workdir, server, devnet

    async def create(self) -> None: ...
    async def sync(self) -> None: ...
    async def restart(self) -> None: ...
    async def address(self) -> str: ...
    async def send(self, to: str, zatoshis: int) -> None: ...


async def run_driver(driver_cls: type[Driver], devnet: Devnet, recipient: str) -> Run:
    proxy = AuditProxy("127.0.0.1:0", LWD.removeprefix("http://"))
    await proxy.start()
    workdir = Path(tempfile.mkdtemp(prefix=f"report-{driver_cls.name}-"))
    driver = driver_cls(workdir, proxy.url, devnet)
    run = Run(driver.name, driver.version)

    async def scenario(name: str, action: Callable[[], Awaitable[None]]) -> None:
        start = len(proxy.records)
        await action()
        run.records[name] = proxy.records[start:]

    async def receive() -> None:
        devnet.pay(await driver.address(), PAYMENT)
        devnet.mine(10)  # spendable for the send scenario
        await driver.sync()

    async def restart() -> None:
        devnet.mine(2)
        await driver.restart()
        await driver.sync()

    try:
        await scenario("create", driver.create)
        await scenario("first-sync", driver.sync)
        await scenario("receive", receive)
        await scenario("restart", restart)
        await scenario("send", lambda: driver.send(recipient, SEND))
    except Exception as exc:  # a wallet that cannot finish is reported, not hidden
        run.notes.append(f"stopped early: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        await proxy.stop()
        shutil.rmtree(workdir, ignore_errors=True)
    return run


# --- drivers -------------------------------------------------------------------------


class ZecPy(Driver):
    name = "ZecPy"
    version = "0.1.0 (this repo)"

    async def create(self) -> None:
        from zecpy import Wallet

        self.wallet, self.mnemonic = await Wallet.create(
            self.workdir / "w", network="regtest", server=self.server,
            regtest_heights=self.devnet.heights,
        )

    async def sync(self) -> None:
        await self.wallet.sync()

    async def restart(self) -> None:
        from zecpy import Wallet

        self.wallet = Wallet.open(self.workdir / "w", server=self.server, mnemonic=self.mnemonic)

    async def address(self) -> str:
        return self.wallet.address()

    async def send(self, to: str, zatoshis: int) -> None:
        await self.wallet.send(to, zatoshis)


async def _exec(*argv: str, stdin: bytes = b"", cwd: Path | None = None, timeout: float = 600) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(argv[0]).name} {' '.join(argv[1:4])}... failed: {err.decode()[-400:]}")
    return out.decode() + err.decode()


class Devtool(Driver):
    name = "zcash-devtool"
    version = "0b3436b (librustzcash sync::run, transparent support on)"

    def _cmd(self, *args: str) -> list[str]:
        return [str(DEV / "bin" / "zcash-devtool"), "wallet", "-w", str(self.workdir / "w"), *args]

    async def create(self) -> None:
        await _exec(
            *self._cmd("init", "--name", "report", "--identity", str(self.workdir / "id.age"),
                       "-n", "regtest", "--activation-heights",
                       str(REPO / "devnet" / "activation-heights.toml"),
                       "-s", self.server.removeprefix("http://")),
            stdin=b"\n",
        )

    async def sync(self) -> None:
        await _exec(*self._cmd("sync", "-s", self.server.removeprefix("http://")))

    async def restart(self) -> None:
        pass  # every devtool command is a fresh process

    async def address(self) -> str:
        out = await _exec(*self._cmd("generate-address"))
        return next(w for w in out.split() if w.strip('"(),').startswith("uregtest1")).strip('"(),')

    async def send(self, to: str, zatoshis: int) -> None:
        await _exec(*self._cmd("send", "-i", str(self.workdir / "id.age"), "--address", to,
                               "--value", str(zatoshis), "-s", self.server.removeprefix("http://")))


DRIVERS: list[type[Driver]] = [ZecPy, Devtool]


# --- report --------------------------------------------------------------------------


def _cell(records: list[Record]) -> str:
    bad = [r for r in records if r.label is Label.IDENTIFYING]
    if not records:
        return "no requests"
    if not bad:
        return f"0 of {len(records)}"
    methods = sorted({r.method for r in bad})
    return f"**{len(bad)} of {len(records)}**: {', '.join(methods)}"


def write_report(runs: list[Run]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for run in runs:
        with (OUT_DIR / f"{run.wallet}.jsonl").open("w") as log:
            for scenario, records in run.records.items():
                for r in records:
                    log.write(json.dumps({"scenario": scenario, **json.loads(r.to_json())}) + "\n")

    lines = [
        "# Light Wallet Privacy Report",
        "",
        f"Generated by `python -m tools.leak_audit.report` on {time.strftime('%Y-%m-%d')} against a",
        "local regtest chain (devnet/: zebrad 7.0.0, lightwalletd 0.6.0, Ironwood active).",
        "Each wallet runs the same five scenarios through `tools/leak_audit`, which records and",
        "classifies every request the light server receives. Raw logs: `docs/privacy-report/`.",
        "",
        "Cells show **identifying requests** of total requests. An identifying request ties",
        "itself to the wallet's addresses, transactions or exact history; see",
        "`docs/privacy-model.md` for the labels. Lower is better; network-level metadata (IP,",
        "timing) is out of scope here because the proxy runs on loopback.",
        "",
        "| Scenario | " + " | ".join(f"{r.wallet}" for r in runs) + " |",
        "|---|" + "---|" * len(runs),
    ]
    for key, title in SCENARIOS:
        lines.append(f"| {title} | " + " | ".join(_cell(r.records.get(key, [])) for r in runs) + " |")
    total = lambda r: sum(  # noqa: E731
        1 for recs in r.records.values() for x in recs if x.label is Label.IDENTIFYING
    )
    lines.append("| **Total identifying** | " + " | ".join(f"**{total(r)}**" for r in runs) + " |")
    lines += ["", "## Versions", ""]
    lines += [f"- **{r.wallet}**: {r.version}" for r in runs]
    lines += ["", "## What the identifying requests reveal", ""]
    for run in runs:
        reasons: dict[str, set[str]] = {}
        for recs in run.records.values():
            for r in recs:
                if r.label is Label.IDENTIFYING:
                    reasons.setdefault(r.method, set()).add(r.reason)
        lines.append(f"### {run.wallet}")
        lines.append("")
        if not reasons:
            lines.append("No identifying requests.")
        for method, why in sorted(reasons.items()):
            lines.append(f"- `{method}`: {sorted(why)[0]}" + (f" (and {len(why) - 1} similar)" if len(why) > 1 else ""))
        for note in run.notes:
            lines.append(f"- Note: {note}")
        lines.append("")
    path = REPO / "docs" / "privacy-report.md"
    path.write_text("\n".join(lines))
    return path


async def main() -> None:
    devnet = Devnet(LWD, activation_heights())
    devnet.height()  # fails fast if devnet is down
    from zecpy import Wallet

    sink, _ = await Wallet.create(
        Path(tempfile.mkdtemp()) / "sink", network="regtest", server=LWD,
        regtest_heights=devnet.heights,
    )
    recipient = sink.address()
    runs = []
    for driver in DRIVERS:
        print(f"running {driver.name}...", flush=True)
        runs.append(await run_driver(driver, devnet, recipient))
        print("  " + ", ".join(f"{k}: {_cell(v)}" for k, v in runs[-1].records.items()), flush=True)
    print(f"wrote {write_report(runs)}")


if __name__ == "__main__":
    asyncio.run(main())
