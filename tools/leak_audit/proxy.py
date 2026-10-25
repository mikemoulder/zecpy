"""A recording gRPC proxy for lightwalletd's CompactTxStreamer service.

Requests and responses are forwarded as raw bytes, so the proxy cannot change what the
wallet and server exchange; it only decodes a copy of each request for the record.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import grpc
from google.protobuf.json_format import MessageToDict

from .classify import Label, classify
from .schema import METHODS, Method


@dataclass(frozen=True)
class Record:
    time: float
    method: str
    label: Label
    reason: str
    request: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        data["label"] = self.label.value
        return json.dumps(data, sort_keys=True)


def _summarise(method: Method, raw: bytes) -> dict[str, Any]:
    message = method.request.FromString(raw)
    data = MessageToDict(message, preserving_proto_field_name=True)
    # Raw transactions are large and say nothing the label does not; keep their size only.
    if method.name == "SendTransaction" and "data" in data:
        data = {"data_bytes": len(message.data), "height": data.get("height")}
    return data


class AuditProxy:
    """Listens on `listen`, forwards to `upstream`, and records every request."""

    def __init__(
        self,
        listen: str,
        upstream: str,
        *,
        upstream_tls: bool = False,
        tls_name: str | None = None,
        log_path: Path | None = None,
        on_record: Callable[[Record], None] | None = None,
    ) -> None:
        self.listen = listen
        self.upstream = upstream
        self.upstream_tls = upstream_tls
        # The name the upstream's TLS certificate is checked against, when `upstream` is a
        # local tunnel (for example a SOCKS forwarder into Tor) rather than the server itself.
        self.tls_name = tls_name
        self.log_path = log_path
        self.on_record = on_record
        self.records: list[Record] = []
        self._server: grpc.aio.Server | None = None
        self._channel: grpc.aio.Channel | None = None

    def _record(self, method: Method, raw: bytes) -> None:
        try:
            request = _summarise(method, raw)
        except Exception as exc:  # undecodable requests are recorded, never dropped
            request = {"undecodable": repr(exc)}
        label, reason = classify(method.name, request)
        record = Record(time.time(), method.name, label, reason, request)
        self.records.append(record)
        if self.log_path is not None:
            with self.log_path.open("a") as log:
                log.write(record.to_json() + "\n")
        if self.on_record is not None:
            self.on_record(record)

    def _handler(self, method: Method) -> grpc.RpcMethodHandler:
        channel = self._channel
        assert channel is not None

        async def relay_error(context: grpc.aio.ServicerContext, error: grpc.aio.AioRpcError) -> None:
            await context.abort(error.code(), error.details() or "")

        if not method.client_streaming and not method.server_streaming:
            upstream = channel.unary_unary(method.path)

            async def unary_unary(request: bytes, context):
                self._record(method, request)
                try:
                    return await upstream(request)
                except grpc.aio.AioRpcError as e:
                    await relay_error(context, e)

            return grpc.unary_unary_rpc_method_handler(unary_unary)

        if not method.client_streaming:
            upstream = channel.unary_stream(method.path)

            async def unary_stream(request: bytes, context) -> AsyncIterator[bytes]:
                self._record(method, request)
                try:
                    async for response in upstream(request):
                        yield response
                except grpc.aio.AioRpcError as e:
                    await relay_error(context, e)

            return grpc.unary_stream_rpc_method_handler(unary_stream)

        async def recorded(requests: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            async for request in requests:
                self._record(method, request)
                yield request

        if not method.server_streaming:
            upstream = channel.stream_unary(method.path)

            async def stream_unary(requests, context):
                try:
                    return await upstream(recorded(requests))
                except grpc.aio.AioRpcError as e:
                    await relay_error(context, e)

            return grpc.stream_unary_rpc_method_handler(stream_unary)

        upstream = channel.stream_stream(method.path)

        async def stream_stream(requests, context) -> AsyncIterator[bytes]:
            try:
                async for response in upstream(recorded(requests)):
                    yield response
            except grpc.aio.AioRpcError as e:
                await relay_error(context, e)

        return grpc.stream_stream_rpc_method_handler(stream_stream)

    async def start(self) -> None:
        if self.upstream_tls:
            options = [("grpc.ssl_target_name_override", self.tls_name)] if self.tls_name else []
            self._channel = grpc.aio.secure_channel(
                self.upstream, grpc.ssl_channel_credentials(), options=options
            )
        else:
            self._channel = grpc.aio.insecure_channel(self.upstream)
        handlers = {name: self._handler(m) for name, m in METHODS.items()}
        service = grpc.method_handlers_generic_handler(
            "cash.z.wallet.sdk.rpc.CompactTxStreamer", handlers
        )
        self._server = grpc.aio.server()
        self._server.add_generic_rpc_handlers((service,))
        port = self._server.add_insecure_port(self.listen)
        if port == 0:
            raise OSError(f"cannot listen on {self.listen}")
        self.listen = f"{self.listen.rsplit(':', 1)[0]}:{port}"  # resolves a requested port 0
        await self._server.start()

    @property
    def url(self) -> str:
        """The address to give a wallet as its server."""
        return f"http://{self.listen}"

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=1)
        if self._channel is not None:
            await self._channel.close()

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {label.value: 0 for label in Label}
        for record in self.records:
            counts[record.label.value] += 1
        return counts
