import socket
import threading
import unittest
from datetime import datetime, timezone

from miio.protocol import Message

from haus_kettle.model import CommandError, PROPERTIES, plan
from haus_kettle.transport import Transport
from test_model import reading


class UdpKettle:
    """Actual encrypted UDP peer; synthetic values, never a household device."""

    def __init__(self):
        self.token = bytes.fromhex("a" * 32)
        self.channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.channel.bind(("127.0.0.1", 0))
        self.channel.settimeout(0.1)
        self.stopped = threading.Event()
        self.requests = []
        self.reject = False
        self.identity = 123
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopped.is_set():
            try:
                data, peer = self.channel.recvfrom(4096)
            except socket.timeout:
                continue
            timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
            if len(data) == 32:
                header = bytes.fromhex("2131002000000000") + self.identity.to_bytes(4, "big")
                self.channel.sendto(
                    header
                    + int(timestamp.replace(tzinfo=timezone.utc).timestamp()).to_bytes(4, "big")
                    + bytes(16),
                    peer,
                )
                continue
            request = Message.parse(data, token=self.token).data.value
            self.requests.append(request)
            if request["method"] == "get_properties":
                values = {ids: reading().get(key) for key, ids in PROPERTIES.items()}
                result = [
                    {
                        **prop,
                        "code": 0
                        if values.get((prop["siid"], prop["piid"])) is not None
                        else -4003,
                        "value": values.get((prop["siid"], prop["piid"])),
                    }
                    for prop in request["params"]
                ]
            else:
                result = [{"code": -1 if self.reject else 0}]
            packet = Message.build(
                {
                    "header": {
                        "value": {
                            "length": 0,
                            "device_id": self.identity.to_bytes(4, "big"),
                            "ts": timestamp,
                        }
                    },
                    "data": {"value": {"id": request["id"], "result": result}},
                    "checksum": 0,
                },
                token=self.token,
            )
            self.channel.sendto(packet, peer)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
        self.channel.close()


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.peer = UdpKettle()
        self.addCleanup(self.peer.close)
        self.transport = Transport({"host": "127.0.0.1", "token": "a" * 32, "did": "123"})
        self.transport.port = self.peer.channel.getsockname()[1]

    def test_actual_encrypted_read_and_reboot_handshake_never_write(self):
        for _ in range(2):
            self.assertEqual(self.transport.read()["current_temperature"], 21)
        self.assertEqual(len(self.peer.requests), 2 * ((len(PROPERTIES) + 7) // 8))
        self.assertTrue(all(r["method"] == "get_properties" for r in self.peer.requests))
        self.assertTrue(all(len(r["params"]) <= 8 for r in self.peer.requests))

    def test_device_identity_is_checked_before_any_request(self):
        self.peer.identity = 456
        with self.assertRaises(CommandError):
            self.transport.read()
        self.assertEqual(self.peer.requests, [])

    def test_failed_first_write_prevents_activation_and_is_not_retried(self):
        self.transport.read()
        self.peer.requests.clear()
        self.peer.reject = True
        with self.assertRaises(CommandError):
            self.transport.write(plan("boil", "PRESS", reading()))
        self.assertEqual(len(self.peer.requests), 1)
        self.assertEqual(self.peer.requests[0]["params"][0]["piid"], 11)

    def test_stop_action_uses_object_parameters(self):
        self.transport.read()
        self.transport.write(plan("stop", "PRESS", {}))
        self.assertEqual(self.peer.requests[-1]["method"], "action")
        self.assertEqual(
            self.peer.requests[-1]["params"], {"did": "123", "siid": 3, "aiid": 1, "in": []}
        )
