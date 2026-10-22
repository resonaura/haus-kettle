import unittest

from haus_kettle.discovery import discovery
from haus_kettle.model import PROPERTIES, CommandError, decode, plan


def reading(**patch):
    return {
        "status": 0,
        "current_temperature": 21,
        "target_temperature": 80,
        "power": False,
        "fault": 0,
        "lifted": False,
        "target_mode": 0,
        "activity": "Idle",
        "mode": "off",
        **patch,
    }


def rows(state=None):
    return [
        {"siid": PROPERTIES[key][0], "piid": PROPERTIES[key][1], "code": 0, "value": value}
        for key, value in (state or reading()).items()
        if key in PROPERTIES
    ]


class ModelTest(unittest.TestCase):
    def test_observations_are_not_optimistic_or_fake_temperatures(self):
        self.assertEqual(decode(rows())["mode"], "off")
        self.assertEqual(
            decode(rows(reading(power=True, status=2, target_mode=1)))["mode"], "performance"
        )
        self.assertEqual(decode(rows(reading(power=True, status=0, target_mode=1)))["mode"], "off")
        self.assertEqual(decode(rows(reading(power=True, status=4)))["activity"], "Keeping warm")
        for data in [
            [],
            rows(reading(current_temperature=200)),
            rows(reading(current_temperature=True)),
        ]:
            with self.assertRaises(CommandError):
                decode(data)

    def test_boiling_selects_mode_then_activates_without_destroying_presets(self):
        operations = plan("boil", "PRESS", reading())
        self.assertEqual(
            [p.params for p in operations],
            [({"siid": 3, "piid": 11, "value": 1},), ({"siid": 2, "piid": 7, "value": True},)],
        )
        self.assertEqual(plan("mode", "electric", reading())[0].params[0]["value"], 0)

    def test_stop_is_an_action_even_if_current_state_is_stale(self):
        operations = plan("mode", "off", {}, age=1000)
        self.assertEqual(operations[0].method, "action")
        self.assertEqual(operations[1].params, ({"siid": 2, "piid": 7, "value": False},))

    def test_no_activation_without_fresh_safety_readings(self):
        for state, age in [
            (reading(), 16),
            (reading(fault=1), 0),
            (reading(lifted=True), 0),
            ({}, 0),
        ]:
            with self.assertRaises(CommandError):
                plan("boil", "PRESS", state, age=age)

    def test_read_only_blocks_every_write(self):
        for key, value in [
            ("boil", "PRESS"),
            ("stop", "PRESS"),
            ("target_temperature", "45"),
            ("quiet", "ON"),
        ]:
            with self.assertRaises(CommandError):
                plan(key, value, reading(), read_only=True)

    def test_invalid_values_cannot_produce_writes(self):
        for key, value in [
            ("target_temperature", "100"),
            ("target_temperature", "nan"),
            ("target_temperature", "inf"),
            ("target_temperature", "45.1"),
            ("keep_warm_minutes", "61"),
            ("quiet", "yes"),
            ("arbitrary", "1"),
        ]:
            with self.assertRaises(CommandError):
                plan(key, value, reading())

    def test_temperature_change_does_not_activate(self):
        self.assertEqual(
            plan("target_temperature", "45", reading())[0].params,
            ({"siid": 2, "piid": 4, "value": 45},),
        )

    def test_buttons_reject_arbitrary_payloads(self):
        for key in ["boil", "stop"]:
            with self.assertRaises(CommandError):
                plan(key, "NO", reading())

    def test_mqtt_discovery_groups_all_capabilities_and_keeps_commands_unretained(self):
        topic, config = discovery("123", "Kitchen Kettle")
        self.assertEqual(topic, "homeassistant/device/haus_kettle_123/config")
        self.assertEqual(config["device"]["identifiers"], ["haus_kettle_123"])
        components = config["components"]
        self.assertEqual(components["kettle"]["platform"], "water_heater")
        self.assertEqual(components["kettle"]["max_temp"], 99)
        self.assertIn("boil", components)
        for component in components.values():
            if any("command_topic" in k for k in component):
                self.assertFalse(component["retain"])
                self.assertEqual(component["qos"], 1)
        _, monitor = discovery("123", "Kitchen Kettle", read_only=True)
        self.assertNotIn("boil", monitor["components"])
        self.assertNotIn("mode_command_topic", monitor["components"]["kettle"])
