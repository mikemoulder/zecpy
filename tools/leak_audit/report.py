"""Light Wallet Privacy Report: the same five scenarios, run by each wallet through the
audit proxy on the local regtest chain, with every request recorded and classified.

    python -m tools.leak_audit.report              # needs devnet/up.sh running
    python -m tools.leak_audit.report --from-logs  # rebuild the document from the raw logs

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
    version = "0b3436b (its own sync loop on the librustzcash crates, transparent support on)"

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


class Zingo(Driver):
    name = "zingo-cli"
    version = (
        "zingolib 625f617 (pepper-sync), built with its `nakednet-test-mode` measurement feature "
        "and without `nym`: by default zingo-cli sends over the Nym mixnet, which a loopback "
        "proxy cannot observe. Driven through its interactive prompt; each scenario is a new process"
    )

    async def _session(self, *commands: str, sync: bool, done: str | None = None, timeout: float = 300) -> str:
        """Runs one zingo-cli session: optionally `sync run` until the scan reaches 100%, then
        the given commands (waiting for `done` to appear in the output), then `quit`."""
        proc = await asyncio.create_subprocess_exec(
            str(DEV / "bin" / "zingo-cli-measure"), "--chain", "regtest", "--server", self.server,
            "--data-dir", str(self.workdir / "w"), "--nosync",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out: list[str] = []

        async def read() -> None:
            assert proc.stdout is not None
            while line := await proc.stdout.readline():
                out.append(line.decode())

        async def say(command: str) -> None:
            assert proc.stdin is not None
            proc.stdin.write(command.encode() + b"\n")
            await proc.stdin.drain()

        async def wait_for(marker: str) -> None:
            mark = len(out)
            while marker not in "".join(out[mark:]):
                await asyncio.sleep(0.5)

        reader = asyncio.create_task(read())
        try:
            async with asyncio.timeout(timeout):
                if sync:
                    await say("sync run")
                    while True:
                        mark = len(out)
                        await say("sync status")
                        await asyncio.sleep(2)
                        if '"percentage_total_blocks_scanned": 100' in "".join(out[mark:]):
                            break
                for command in commands:
                    await say(command)
                    if done:
                        await wait_for(done)
                await say("quit")
                await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
            await reader
        return "".join(out)

    async def create(self) -> None:
        (self.workdir / "w").mkdir()
        await self._session("addresses", sync=False, done="encoded_address")

    async def sync(self) -> None:
        await self._session(sync=True)

    async def restart(self) -> None:
        pass  # every scenario is already a fresh zingo-cli process

    async def address(self) -> str:
        out = await self._session("addresses", sync=False, done="encoded_address")
        for word in out.replace('"', " ").replace(",", " ").split():
            if word.startswith("uregtest1"):
                return word
        raise RuntimeError(f"no regtest address in zingo-cli output: {out[-300:]}")

    async def send(self, to: str, zatoshis: int) -> None:
        out = await self._session(f"quicksend {to} {zatoshis}", sync=False, done="txid")
        if "error" in out.lower() and "txid" not in out:
            raise RuntimeError(f"zingo-cli quicksend failed: {out[-300:]}")


DRIVERS: list[type[Driver]] = [ZecPy, Devtool, Zingo]


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
    lines += [
        "",
        "## Reading this fairly",
        "",
        "- **Scope.** This measures what request *content* reveals. It does not measure who is",
        "  connecting: zingo-cli sends over the Nym mixnet by default, and ZecPy sends and syncs",
        "  over Tor by default. Neither protection is visible to a loopback proxy, so both were",
        "  measured without them.",
        "- **Transparent support is a trade-off.** zingo-cli and zcash-devtool can receive to",
        "  transparent addresses, and asking the server about those addresses",
        "  (`GetTaddressTxids`, `GetAddressUtxosStream`) is how a light wallet finds such funds.",
        "  ZecPy is shielded-only by design: it never issues these requests, and it cannot",
        "  receive transparent funds.",
        "- **Memos.** zingo-cli fetches received transactions by txid (`GetTransaction`), for example",
        "  to read their memos. ZecPy does not read incoming memos; its invoices match payments by",
        "  receiving address instead.",
        "- **Counts depend on the chain.** zingo-cli's transparent lookups scale with its",
        "  address gap limit; tree-state requests scale with the number of sync batches.",
    ]
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


def load_runs() -> list[Run]:
    """Rebuilds the runs from the raw logs, to regenerate the document without rerunning."""
    runs = []
    for driver in DRIVERS:
        log = OUT_DIR / f"{driver.name}.jsonl"
        if not log.exists():
            continue
        run = Run(driver.name, driver.version)
        for line in log.read_text().splitlines():
            data = json.loads(line)
            record = Record(data["time"], data["method"], Label(data["label"]), data["reason"], data["request"])
            run.records.setdefault(data["scenario"], []).append(record)
        runs.append(run)
    return runs


async def main() -> None:
    import sys

    if "--from-logs" in sys.argv:
        print(f"wrote {write_report(load_runs())}")
        return

    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    drivers = [d for d in DRIVERS if only is None or d.name == only]
    devnet = Devnet(LWD, activation_heights())
    devnet.height()  # fails fast if devnet is down
    from zecpy import Wallet

    sink, _ = await Wallet.create(
        Path(tempfile.mkdtemp()) / "sink", network="regtest", server=LWD,
        regtest_heights=devnet.heights,
    )
    recipient = sink.address()
    runs = []
    for driver in drivers:
        print(f"running {driver.name}...", flush=True)
        runs.append(await run_driver(driver, devnet, recipient))
        print("  " + ", ".join(f"{k}: {_cell(v)}" for k, v in runs[-1].records.items()), flush=True)
    if only is None:
        print(f"wrote {write_report(runs)}")
    else:
        for run in runs:
            print("notes:", run.notes)


if __name__ == "__main__":
    asyncio.run(main())
