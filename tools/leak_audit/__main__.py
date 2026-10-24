"""Run the audit proxy and print each request as it happens.

    python -m tools.leak_audit --listen 127.0.0.1:19167 --upstream 127.0.0.1:19067

Point a wallet at the listen address. Ctrl-C prints a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .classify import Label
from .proxy import AuditProxy, Record

_COLOUR = {
    Label.PUBLIC: "\033[32m",  # green
    Label.COARSE: "\033[36m",  # cyan
    Label.BROADCAST: "\033[33m",  # yellow
    Label.IDENTIFYING: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


def _print(record: Record) -> None:
    stamp = time.strftime("%H:%M:%S", time.localtime(record.time))
    detail = json.dumps(record.request, sort_keys=True)
    if len(detail) > 70:
        detail = detail[:67] + "..."
    colour = _COLOUR[record.label]
    print(f"{stamp}  {colour}{record.label.value:<11}{_RESET}  {record.method:<24} {detail}")
    print(f"{'':10} {record.reason}", flush=True)


async def _main(args: argparse.Namespace) -> None:
    proxy = AuditProxy(
        args.listen,
        args.upstream,
        upstream_tls=args.tls,
        log_path=Path(args.log) if args.log else None,
        on_record=_print,
    )
    await proxy.start()
    print(f"auditing {args.listen} -> {args.upstream}  (Ctrl-C for summary)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await proxy.stop()
        counts = proxy.summary()
        print("\n" + "  ".join(f"{k}: {v}" for k, v in counts.items()))
        verdict = "LEAKS FOUND" if counts["identifying"] else "no identifying requests"
        print(verdict)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.leak_audit", description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:19167")
    parser.add_argument("--upstream", default="127.0.0.1:19067")
    parser.add_argument("--tls", action="store_true", help="upstream uses TLS")
    parser.add_argument("--log", help="append JSON lines to this file")
    try:
        asyncio.run(_main(parser.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
