"""Bounded local IO. A failed write is never retried, especially an activation."""

import secrets
import socket
import time
from datetime import datetime, timedelta, timezone

from miio.protocol import Message

from .model import PROPERTIES, CommandError, decode


class Transport:
    def __init__(self, credentials):
        self.did = str(credentials["did"])
        self.host = credentials["host"]
        self.token = bytes.fromhex(credentials["token"])
        self.port = 54321
        self.sequence = secrets.randbelow(9000) + 1
        self.timestamp = datetime.now(timezone.utc).replace(tzinfo=None)

    def handshake(self):
        hello = bytes.fromhex("21310020" + "f" * 56)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            channel.settimeout(1)
            channel.sendto(hello, (self.host, self.port))
            data, peer = channel.recvfrom(4096)
        if (
            peer[0] != self.host
            or len(data) != 32
            or data[:2] != b"!1"
            or int.from_bytes(data[8:12], "big") != int(self.did)
        ):
            raise CommandError("The local device does not match the selected kettle.")
        self.timestamp = datetime.fromtimestamp(
            int.from_bytes(data[12:16], "big"), timezone.utc
        ).replace(tzinfo=None)

    def send(self, method, params):
        self.sequence = self.sequence % 9999 + 1
        header = {
            "length": 0,
            "device_id": int(self.did).to_bytes(4, "big"),
            "ts": self.timestamp + timedelta(seconds=1),
        }
        packet = Message.build(
            {
                "header": {"value": header},
                "data": {"value": {"id": self.sequence, "method": method, "params": params}},
                "checksum": 0,
            },
            token=self.token,
        )
        deadline = time.monotonic() + 2
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            channel.sendto(packet, (self.host, self.port))
            while True:
                channel.settimeout(max(0.001, deadline - time.monotonic()))
                data, peer = channel.recvfrom(4096)
                if peer != (self.host, self.port):
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    continue
                reply = Message.parse(data, token=self.token)
                payload = reply.data.value
                if payload.get("id") != self.sequence or int.from_bytes(
                    reply.header.value.device_id, "big"
                ) != int(self.did):
                    raise CommandError("The kettle returned an unexpected response.")
                if "error" in payload or "result" not in payload:
                    raise CommandError("The kettle did not accept the request.")
                self.timestamp = reply.header.value.ts
                return payload["result"]

    def read(self):
        self.handshake()
        properties = [
            {"did": self.did, "siid": siid, "piid": piid} for siid, piid in PROPERTIES.values()
        ]
        rows = []
        for offset in range(0, len(properties), 8):
            rows.extend(self.send("get_properties", properties[offset : offset + 8]))
        return decode(rows)

    def write(self, operations, *, cancelled=lambda: False):
        # Stop intentionally skips a state read; still refresh identity and clock.
        if cancelled():
            raise CommandError("A newer request cancelled this command.")
        self.handshake()
        for operation in operations:
            if cancelled():
                raise CommandError("A newer request cancelled this command.")
            params = [{"did": self.did, **item} for item in operation.params]
            result = self.send(
                operation.method, params[0] if operation.method == "action" else params
            )
            rows = result if isinstance(result, list) else [result]
            if not rows or any(not isinstance(row, dict) or row.get("code") != 0 for row in rows):
                raise CommandError(
                    "The kettle did not confirm the command. Its state will be checked again."
                )
