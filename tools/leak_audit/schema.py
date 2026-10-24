"""Message classes for the lightwalletd CompactTxStreamer service, built at import time from
the .proto files in ./proto, so no generated code is checked in."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from grpc_tools import protoc

PROTO_DIR = Path(__file__).resolve().parent / "proto"
SERVICE = "cash.z.wallet.sdk.rpc.CompactTxStreamer"


@dataclass(frozen=True)
class Method:
    name: str
    path: str
    request: type
    client_streaming: bool
    server_streaming: bool


def _load() -> dict[str, Method]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "descriptors.bin"
        status = protoc.main(
            [
                "protoc",
                f"-I{PROTO_DIR}",
                f"--descriptor_set_out={out}",
                "--include_imports",
                str(PROTO_DIR / "service.proto"),
            ]
        )
        if status != 0:
            raise RuntimeError(f"protoc failed with status {status}")
        files = descriptor_pb2.FileDescriptorSet.FromString(out.read_bytes())

    pool = descriptor_pool.DescriptorPool()
    for file in files.file:
        pool.Add(file)
    service = pool.FindServiceByName(SERVICE)
    return {
        m.name: Method(
            name=m.name,
            path=f"/{SERVICE}/{m.name}",
            request=message_factory.GetMessageClass(m.input_type),
            client_streaming=m.client_streaming,
            server_streaming=m.server_streaming,
        )
        for m in service.methods
    }


METHODS: dict[str, Method] = _load()
