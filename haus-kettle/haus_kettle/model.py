"""The xiaomi.kettle.v21 MIoT contract, separate from transport and MQTT."""

import math
from dataclasses import dataclass

MODEL = "xiaomi.kettle.v21"
PROPERTIES = {
    "status": (2, 1),
    "fault": (2, 2),
    "current_temperature": (2, 3),
    "target_temperature": (2, 4),
    "auto_keep_warm": (2, 5),
    "keep_warm_temperature": (2, 6),
    "power": (2, 7),
    "keep_warm_minutes": (3, 1),
    "custom_knob_temperature": (3, 2),
    "remember_temperature": (3, 4),
    "boiling_reminder": (3, 5),
    "keep_warm_reminder": (3, 6),
    "lifted": (3, 7),
    "warming_minutes": (3, 10),
    "target_mode": (3, 11),
    "quiet": (6, 1),
}
SWITCHES = {
    "auto_keep_warm",
    "custom_knob_temperature",
    "remember_temperature",
    "boiling_reminder",
    "keep_warm_reminder",
    "quiet",
}
NUMBERS = {
    "target_temperature": (40, 99, 1, "°C"),
    "keep_warm_temperature": (40, 99, 1, "°C"),
    "keep_warm_minutes": (60, 1440, 30, "min"),
}
STATUSES = {0: "Idle", 1: "Heating", 2: "Boiling", 3: "Cooling", 4: "Keeping warm"}


class CommandError(ValueError):
    """A fixed, safe message intended for a diagnostic sensor, never raw IO errors."""


@dataclass(frozen=True)
class Operation:
    method: str
    params: tuple


def number(value, low, high, step=1):
    if isinstance(value, bool):
        raise CommandError("Choose a valid number.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise CommandError("Choose a valid number.") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise CommandError("The value is outside the supported range.")
    if abs((result - low) / step - round((result - low) / step)) > 1e-6:
        raise CommandError("The value does not match the supported step.")
    return int(result)


def decode(rows):
    reverse = {ids: key for key, ids in PROPERTIES.items()}
    state = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("code") != 0:
            continue
        key = reverse.get((row.get("siid"), row.get("piid")))
        value = row.get("value")
        if key in SWITCHES | {"power", "lifted"}:
            if isinstance(value, bool):
                state[key] = value
        elif key and isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value):
                state[key] = value
    if not {"status", "current_temperature", "power"} <= state.keys():
        raise CommandError("The kettle did not return a complete reading.")
    if state["status"] not in STATUSES or not -30 <= state["current_temperature"] <= 100:
        raise CommandError("The kettle returned an invalid reading.")
    state["activity"] = STATUSES[state["status"]]
    state["mode"] = (
        "off"
        if not state["power"]
        else "performance"
        if state.get("target_mode") == 1 or state["status"] == 2
        else "electric"
    )
    return state


def _set(key, value):
    siid, piid = PROPERTIES[key]
    return Operation("set_properties", ({"siid": siid, "piid": piid, "value": value},))


def plan(key, payload, state, *, read_only=False, age=0):
    """Validate a fresh explicit request. Activation always comes last; never retry it."""
    if read_only:
        raise CommandError("Read-only commissioning is enabled in the app configuration.")
    if not isinstance(payload, str) or len(payload) > 128:
        raise CommandError("Unsupported command.")
    value = payload.strip()
    if key == "stop" or key in {"mode", "power"} and value in {"off", "OFF"}:
        return [Operation("action", ({"siid": 3, "aiid": 1, "in": []},))]
    if key in {"boil", "mode", "power"}:
        mode = "performance" if key == "boil" else value
        if key == "power" and value == "ON":
            mode = "electric"
        if mode not in {"performance", "electric"}:
            raise CommandError("Choose Heat, Boil or Off.")
        if age > 15 or not {"fault", "lifted", "power"} <= state.keys():
            raise CommandError("Wait for a fresh kettle reading before heating.")
        if state["fault"] != 0 or state["lifted"]:
            raise CommandError("Check the kettle and put it on its base before heating.")
        # target-mode 0 = heat, 1 = boil. Do not overwrite the user's warm/knob presets.
        return [_set("target_mode", int(mode == "performance")), _set("power", True)]
    if key in SWITCHES:
        if value not in {"ON", "OFF"}:
            raise CommandError("Choose On or Off.")
        return [_set(key, value == "ON")]
    if key in NUMBERS:
        return [_set(key, number(value, *NUMBERS[key][:3]))]
    raise CommandError("Unsupported command.")
