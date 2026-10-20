import json
import unittest
from types import SimpleNamespace

from haus_kettle.bridge import Bridge
from test_model import reading


class Client:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, **options):
        self.messages.append((topic, payload, options))

    def subscribe(self, _topics):
        pass


class Device:
    did = "123"

    def __init__(self):
        self.fail = False
        self.writes = []

    def read(self):
        if self.fail:
            raise TimeoutError("secret token must never appear in diagnostics")
        return reading()

    def write(self, operations):
        self.writes.append(operations)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.client, self.device = Client(), Device()
        self.bridge = Bridge(self.client, self.device, {}, clock=lambda: self.now)
        self.bridge.on_connect(None, None, None, 0, None)

    def send(self, key="boil", value=b"PRESS", **flags):
        self.bridge.on_message(
            None,
            None,
            SimpleNamespace(
                topic=f"haus/kettle/123/set/{key}",
                payload=value,
                retain=flags.get("retain", False),
                dup=flags.get("dup", False),
            ),
        )

    def test_startup_and_reconnect_never_write(self):
        self.bridge.tick()
        self.bridge.on_disconnect()
        self.bridge.on_connect(None, None, None, 0, None)
        self.bridge.tick()
        self.assertEqual(self.device.writes, [])
        self.assertEqual(len([t for t, _, _ in self.client.messages if t.endswith("/config")]), 2)

    def test_retained_duplicate_old_and_excess_commands_are_not_replayed(self):
        self.send(retain=True)
        self.send(dup=True)
        self.assertTrue(self.bridge.commands.empty())
        for _ in range(100):
            self.send()
        self.assertEqual(self.bridge.commands.qsize(), 8)
        self.now += 5
        for _ in range(8):
            self.bridge.tick()
        self.assertEqual(self.device.writes, [])

    def test_disconnect_discards_commands(self):
        self.send()
        self.bridge.on_disconnect()
        self.assertTrue(self.bridge.commands.empty())

    def test_read_only_never_sends_even_fresh_commands(self):
        self.bridge.read_only = True
        self.send()
        self.bridge.tick()
        self.assertEqual(self.device.writes, [])

    def test_failures_preserve_last_state_and_recover_without_activation(self):
        self.bridge.tick()
        self.device.fail = True
        self.now += 11
        self.bridge.tick()
        self.assertEqual(self.bridge.state["current_temperature"], 21)
        self.assertEqual(self.bridge.next_poll, self.now + 5)
        self.send()
        self.bridge.tick()
        self.assertEqual(self.device.writes, [])
        self.assertNotIn("secret token", json.dumps(self.client.messages))
        self.device.fail = False
        self.now += 61
        self.bridge.tick()
        self.assertEqual(self.bridge.failures, 0)
        self.assertEqual(self.device.writes, [])

    def test_explicit_command_runs_once_after_read_and_does_not_retry_on_ack_loss(self):
        self.send()
        self.bridge.tick()
        self.assertEqual(len(self.device.writes), 1)
        self.bridge.tick()
        self.assertEqual(len(self.device.writes), 1)

    def test_birth_rediscovery_does_not_send_commands(self):
        self.bridge.tick()
        self.bridge.on_message(
            None, None, SimpleNamespace(topic="homeassistant/status", payload=b"online")
        )
        self.bridge.tick()
        self.assertEqual(len([t for t, _, _ in self.client.messages if t.endswith("/config")]), 2)
        self.assertEqual(self.device.writes, [])

    def test_stop_discards_pending_activation_and_does_not_require_a_successful_read(self):
        self.send()
        self.send("stop")
        self.device.fail = True
        self.bridge.tick()
        self.assertEqual(len(self.device.writes), 1)
        self.assertEqual(self.device.writes[0][0].method, "action")
        self.assertTrue(self.bridge.commands.empty())

    def test_failed_write_remains_visible_after_a_successful_poll_without_retrying(self):
        def reject(_operations):
            raise TimeoutError("private source details")

        self.device.write = reject
        self.send()
        self.bridge.tick()
        self.assertIn("not be repeated", self.bridge.command_error)
        self.assertNotIn("private source", json.dumps(self.client.messages))
        diagnostic = [
            json.loads(p) for t, p, _ in self.client.messages if t.endswith("/diagnostic")
        ][-1]
        self.assertEqual(diagnostic["state"], "Connected")
        self.assertEqual(diagnostic["last_command_error"], self.bridge.command_error)
