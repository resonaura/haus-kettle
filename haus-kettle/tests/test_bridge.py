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

    def write(self, operations, *, cancelled=lambda: False):
        if cancelled():
            raise TimeoutError("Cancelled")
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

    def test_redelivered_stop_is_safe_and_cancels_queued_activation(self):
        self.send()
        self.send("mode", b"off", dup=True)
        self.bridge.tick()
        self.assertEqual(len(self.device.writes), 1)
        self.assertFalse(self.device.writes[0][-1].params[0]["value"])
        self.assertTrue(self.bridge.commands.empty())

    def test_status_polls_every_two_seconds_idle_and_one_second_while_active(self):
        self.bridge.tick()
        self.assertEqual(self.bridge.next_poll, self.now + 2)
        self.device.read = lambda: reading(power=True, status=1, mode="electric")
        self.now += 2
        self.bridge.tick()
        self.assertEqual(self.bridge.next_poll, self.now + 1)

    def test_transport_ack_waits_for_matching_state_then_settles_without_rewriting(self):
        self.send("mode", b"electric")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "waiting")
        self.assertEqual(self.bridge.last_command["expected"], {"mode": "electric"})
        self.assertEqual(self.bridge.state["mode"], "off")
        self.assertEqual(self.bridge.next_poll, self.now + 1)
        self.device.read = lambda: reading(
            power=True, status=1, mode="electric", activity="Heating"
        )
        self.now += 1
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "observed")
        self.assertIsNone(self.bridge.confirm_by)
        self.assertEqual(len(self.device.writes), 1)

    def test_stop_is_not_confirmed_by_a_stale_boiling_or_latched_power_reading(self):
        self.device.read = lambda: reading(
            power=True, status=2, mode="performance", activity="Boiling"
        )
        self.send("mode", b"off")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "waiting")
        self.device.read = lambda: reading(power=True)
        self.now += 1
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "waiting")
        self.device.read = lambda: reading()
        self.now += 1
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "observed")
        self.assertEqual(len(self.device.writes), 1)

    def test_unmatched_commands_finish_after_deadline_and_keep_actual_state(self):
        self.send()
        self.bridge.tick()
        self.now += 12
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")
        self.assertIsNone(self.bridge.confirm_by)
        self.assertEqual(self.bridge.state["mode"], "off")
        self.assertIn("not reported", self.bridge.command_error)
        attributes = [
            json.loads(p) for t, p, _ in self.client.messages if t.endswith("/attributes")
        ]
        self.assertEqual(attributes[-1]["last_command"]["status"], "unconfirmed")
        self.assertEqual(len(self.device.writes), 1)

    def test_lost_ack_can_be_confirmed_by_actual_state_without_retry(self):
        def write_then_lose_ack(operations, **_kwargs):
            self.device.writes.append(operations)
            self.device.read = lambda: reading(
                power=True, status=2, target_mode=1, mode="performance", activity="Boiling"
            )
            raise TimeoutError("private packet")

        self.device.write = write_then_lose_ack
        self.send()
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "observed")
        self.assertEqual(self.bridge.last_command["acknowledgement"], "unconfirmed")
        self.assertEqual(self.bridge.command_error, "")
        self.assertEqual(len(self.device.writes), 1)
        self.assertNotIn("private packet", json.dumps(self.client.messages))

    def test_lost_ack_publishes_waiting_until_a_terminal_deadline_or_observation(self):
        def lose_ack(_operations, **_kwargs):
            raise TimeoutError("private packet")

        self.device.write = lose_ack
        self.send()
        self.bridge.tick()
        attrs = [json.loads(p) for t, p, _ in self.client.messages if t.endswith("/attributes")]
        pending = [item["last_command"] for item in attrs if item.get("last_command")]
        self.assertTrue(all(item["status"] != "unconfirmed" for item in pending))
        self.assertEqual(self.bridge.last_command["status"], "waiting")
        self.assertEqual(self.bridge.last_command["acknowledgement"], "unconfirmed")
        self.now += 12
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")

    def test_refresh_bypasses_offline_backoff_and_never_writes_even_read_only(self):
        self.bridge.read_only = True
        self.device.fail = True
        self.bridge.tick()
        self.assertGreater(self.bridge.next_poll, self.now)
        self.device.fail = False
        self.send("refresh")
        self.bridge.tick()
        self.assertEqual(self.bridge.failures, 0)
        self.assertEqual(self.bridge.last_refresh["control"], "refresh")
        self.assertEqual(self.bridge.last_refresh["status"], "observed")
        self.assertIsNone(self.bridge.last_command)
        self.assertEqual(self.device.writes, [])

    def test_refresh_failure_always_finishes_even_with_offline_backoff(self):
        self.device.fail = True
        self.send("refresh")
        self.bridge.tick()
        self.now += 12
        self.bridge.tick()
        self.assertEqual(self.bridge.last_refresh["status"], "unconfirmed")
        self.assertIsNone(self.bridge.refresh_by)
        self.assertEqual(self.device.writes, [])

    def test_refresh_cannot_supersede_or_confirm_pending_heat(self):
        self.send("mode", b"electric")
        self.bridge.tick()
        pending = self.bridge.last_command.copy()
        deadline = self.bridge.confirm_by
        self.send("refresh")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_refresh["status"], "observed")
        self.assertEqual(self.bridge.last_command, pending)
        self.assertEqual(self.bridge.confirm_by, deadline)
        self.assertEqual(self.bridge.last_command["expected"], {"mode": "electric"})
        self.now += 12
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")
        self.assertEqual(self.bridge.last_refresh["status"], "observed")
        self.assertEqual(len(self.device.writes), 1)

    def test_refresh_during_stop_preserves_stop_until_actual_state_matches(self):
        self.device.read = lambda: reading(
            power=True, status=2, mode="performance", activity="Boiling"
        )
        self.send("stop")
        self.bridge.tick()
        pending = self.bridge.last_command.copy()
        self.send("refresh")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_refresh["status"], "observed")
        self.assertEqual(self.bridge.last_command, pending)
        self.device.read = lambda: reading()
        self.now += 1
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["control"], "stop")
        self.assertEqual(self.bridge.last_command["status"], "observed")
        self.assertEqual(len(self.device.writes), 1)

    def test_refresh_does_not_clear_an_unconfirmed_control_error(self):
        self.send()
        self.bridge.tick()
        self.now += 12
        self.bridge.tick()
        previous = self.bridge.last_command.copy()
        error = self.bridge.command_error
        self.send("refresh")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_refresh["status"], "observed")
        self.assertEqual(self.bridge.last_command, previous)
        self.assertEqual(self.bridge.command_error, error)

    def test_refresh_and_control_deadlines_settle_independently_after_read_failure(self):
        self.send()
        self.bridge.tick()
        self.device.fail = True
        self.send("refresh")
        self.bridge.tick()
        self.now += 12
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")
        self.assertEqual(self.bridge.last_refresh["status"], "unconfirmed")
        self.assertIsNone(self.bridge.confirm_by)
        self.assertIsNone(self.bridge.refresh_by)
        self.assertIn("requested state", self.bridge.command_error)
        self.assertIn("fresh readings", self.bridge.refresh_error)

    def test_rejected_and_expired_refresh_leave_control_correlation_intact(self):
        self.send()
        self.bridge.tick()
        pending = self.bridge.last_command.copy()
        deadline = self.bridge.confirm_by
        self.send("refresh", b"private invalid request")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_refresh["status"], "rejected")
        self.assertEqual(self.bridge.last_command, pending)
        self.send("refresh")
        self.now += 5
        self.bridge.tick()
        self.assertEqual(self.bridge.last_refresh["status"], "expired")
        self.assertEqual(self.bridge.last_command, pending)
        self.assertEqual(self.bridge.confirm_by, deadline)
        self.assertNotIn("private invalid request", json.dumps(self.client.messages))

    def test_reconnect_settles_refresh_without_modifying_control_diagnostics(self):
        self.send()
        self.bridge.tick()
        self.device.fail = True
        self.send("refresh")
        self.bridge.tick()
        self.bridge.on_disconnect()
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")
        self.assertEqual(self.bridge.last_refresh["status"], "unconfirmed")
        self.assertEqual(self.bridge.last_command["control"], "boil")
        self.assertIsNone(self.bridge.refresh_by)

    def test_settings_confirm_their_value_not_just_an_arbitrary_read(self):
        self.send("target_temperature", b"45")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "waiting")
        self.device.read = lambda: reading(target_temperature=45)
        self.now += 1
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "observed")

    def test_new_rejected_request_does_not_relabel_the_previous_command(self):
        self.device.read = lambda: reading(target_temperature=45)
        self.send("target_temperature", b"45")
        self.bridge.tick()
        previous = self.bridge.last_command.copy()
        self.send("invalid", b"private payload")
        self.bridge.tick()
        self.assertEqual(previous["status"], "observed")
        self.assertEqual(self.bridge.last_command["sequence"], previous["sequence"] + 1)
        self.assertEqual(self.bridge.last_command["control"], "unsupported")
        self.assertEqual(self.bridge.last_command["status"], "rejected")
        self.assertNotIn("private payload", json.dumps(self.client.messages))

    def test_expired_request_has_diagnostics_and_never_writes(self):
        self.send()
        self.now += 5
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "expired")
        self.assertIn("expired", self.bridge.command_error)
        self.assertEqual(self.device.writes, [])

    def test_rejection_and_expiry_update_heater_attributes_even_if_reads_fail(self):
        self.device.fail = True
        self.bridge.read_only = True
        self.send("stop")
        self.bridge.tick()
        attrs = [json.loads(p) for t, p, _ in self.client.messages if t.endswith("/attributes")]
        self.assertEqual(attrs[-1]["last_command"]["status"], "rejected")
        self.assertIsNone(attrs[-1]["last_seen"])
        self.send()
        self.now += 5
        self.bridge.tick()
        attrs = [json.loads(p) for t, p, _ in self.client.messages if t.endswith("/attributes")]
        self.assertEqual(attrs[-1]["last_command"]["status"], "expired")
        self.assertIn("expired", attrs[-1]["last_command_error"])
        self.assertEqual(self.device.writes, [])

    def test_reconnect_settles_pending_command_without_replaying_it(self):
        self.send()
        self.bridge.tick()
        self.bridge.on_disconnect()
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")
        self.assertIsNone(self.bridge.confirm_by)
        self.bridge.on_connect(None, None, None, 0, None)
        self.bridge.tick()
        self.assertEqual(len(self.device.writes), 1)
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")

    def test_disconnect_during_write_does_not_restart_confirmation_or_publish_online(self):
        def write_and_disconnect(operations, **_kwargs):
            self.device.writes.append(operations)
            self.bridge.on_disconnect()

        self.device.write = write_and_disconnect
        self.send()
        self.bridge.tick()
        self.assertIsNone(self.bridge.confirm_by)
        self.assertEqual(self.bridge.last_command["status"], "unconfirmed")
        availability = [p for t, p, _ in self.client.messages if t.endswith("/availability")]
        self.assertEqual(availability, ["offline", "online"])

    def test_new_stop_supersedes_pending_activation_confirmation(self):
        self.send()
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["status"], "waiting")
        self.send("stop")
        self.bridge.tick()
        self.assertEqual(self.bridge.last_command["control"], "stop")
        self.assertEqual(self.bridge.last_command["status"], "observed")
        self.assertEqual(len(self.device.writes), 2)

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
        def reject(_operations, **_kwargs):
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

    def test_stop_arriving_during_read_cancels_activation_before_write(self):
        def read_and_stop():
            self.send("stop")
            return reading()

        self.device.read = read_and_stop
        self.send()
        self.bridge.tick()
        self.assertEqual(self.device.writes, [])
        self.device.read = lambda: reading()
        self.now += 5
        self.bridge.tick()
        self.assertEqual(self.device.writes[0][0].method, "action")
