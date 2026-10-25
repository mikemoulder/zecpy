"""A local TCP tunnel into a SOCKS5 proxy, so the audit proxy can reach a public light
server over Tor.

    python -m tools.leak_audit.tunnel --listen 127.0.0.1:19443 \\
        --socks 127.0.0.1:9150 --target testnet.zec.rocks:443

Then run the audit proxy with `--upstream 127.0.0.1:19443 --tls --tls-name
testnet.zec.rocks`. The wallet talks to the proxy on loopback, the proxy records every
request, and the request leaves this machine only through Tor. Each connection sends a
different SOCKS username, which arti treats as a request for its own isolated circuit.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import struct

_ids = itertools.count(1)


async def _socks_connect(socks: str, target: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    host, port = socks.rsplit(":", 1)
    reader, writer = await asyncio.open_connection(host, int(port))
    # Username/password authentication only to ask for circuit isolation; arti ignores the values.
    user = f"zecpy-audit-{os.getpid()}-{next(_ids)}".encode()
    writer.write(b"\x05\x01\x02")
    if (await reader.readexactly(2)) != b"\x05\x02":
        raise ConnectionError("SOCKS proxy refused username authentication")
    writer.write(b"\x01" + bytes([len(user)]) + user + b"\x01x")
    if (await reader.readexactly(2))[1] != 0:
        raise ConnectionError("SOCKS authentication failed")
    t_host, t_port = target.rsplit(":", 1)
    name = t_host.encode()
    writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", int(t_port)))
    head = await reader.readexactly(4)
    if head[1] != 0:
        raise ConnectionError(f"SOCKS connect to {target} failed with code {head[1]}")
    skip = {1: 4, 4: 16}.get(head[3])
    if skip is None:
        skip = (await reader.readexactly(1))[0]
    await reader.readexactly(skip + 2)
    return reader, writer


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def serve(listen: str, socks: str, target: str) -> None:
    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await _socks_connect(socks, target)
        except (OSError, ConnectionError, asyncio.IncompleteReadError) as e:
            print(f"tunnel: {e}", flush=True)
            client_w.close()
            return
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w), return_exceptions=True)

    host, port = listen.rsplit(":", 1)
    server = await asyncio.start_server(handle, host, int(port))
    print(f"tunnel {listen} -> {target} via SOCKS {socks}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.leak_audit.tunnel", description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:19443")
    parser.add_argument("--socks", default="127.0.0.1:9150")
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.listen, args.socks, args.target))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
