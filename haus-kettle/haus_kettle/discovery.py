"""One physical MQTT device with water_heater and supplementary capabilities."""

from . import __version__
from .model import MODEL, NUMBERS, SWITCHES


def discovery(device_id, name, *, read_only=False):
    base = f"haus/kettle/{device_id}"
    state = f"{base}/state"
    availability = [{"topic": f"{base}/bridge"}, {"topic": f"{base}/availability"}]
    common = {"availability": availability, "availability_mode": "all", "qos": 0}
    controls = {} if read_only else {"retain": False}
    components = {}

    def add(key, platform, **attributes):
        components[key] = {
            "platform": platform,
            "unique_id": f"haus_kettle_{device_id}_{key}",
            **common,
            **attributes,
        }

    add(
        "kettle",
        "water_heater",
        name=None,
        icon="mdi:kettle-steam-outline",
        modes=["off", "electric", "performance"],
        mode_state_topic=state,
        mode_state_template="{{ value_json.mode }}",
        current_temperature_topic=state,
        current_temperature_template="{{ value_json.current_temperature }}",
        temperature_state_topic=state,
        temperature_state_template="{{ value_json.target_temperature | default(none) }}",
        temperature_unit="C",
        min_temp=40,
        max_temp=99,
        precision=1,
        json_attributes_topic=f"{base}/attributes",
        **(
            {
                "mode_command_topic": f"{base}/set/mode",
                "power_command_topic": f"{base}/set/power",
                "temperature_command_topic": f"{base}/set/target_temperature",
                "temperature_command_template": "{{ value | float | round(0) | int }}",
                **controls,
            }
            if not read_only
            else {}
        ),
    )
    for key, unit in [("current_temperature", "°C"), ("warming_minutes", "min")]:
        add(
            key,
            "sensor",
            name=key.replace("_", " ").capitalize(),
            state_topic=state,
            value_template=f"{{{{ value_json.{key} | default(none) }}}}",
            unit_of_measurement=unit,
            **(
                {"device_class": "temperature", "state_class": "measurement"}
                if unit == "°C"
                else {}
            ),
        )
    add(
        "activity",
        "sensor",
        name="Activity",
        state_topic=state,
        value_template="{{ value_json.activity }}",
    )
    for key in ["lifted", "fault"]:
        add(
            key,
            "binary_sensor",
            name="Lifted from base" if key == "lifted" else "Fault",
            device_class="problem" if key == "fault" else "presence",
            state_topic=state,
            value_template=f"{{{{ 'ON' if value_json.{key} | default(0) else 'OFF' }}}}",
        )
    if not read_only:
        for key in sorted(SWITCHES):
            add(
                key,
                "switch",
                name=key.replace("_", " ").capitalize(),
                state_topic=state,
                value_template=f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
                command_topic=f"{base}/set/{key}",
                entity_category="config",
                **controls,
            )
        for key, (minimum, maximum, step, unit) in NUMBERS.items():
            if key == "target_temperature":
                continue
            add(
                key,
                "number",
                name=key.replace("_", " ").capitalize(),
                state_topic=state,
                value_template=f"{{{{ value_json.{key} | default(none) }}}}",
                command_topic=f"{base}/set/{key}",
                min=minimum,
                max=maximum,
                step=step,
                unit_of_measurement=unit,
                entity_category="config",
                **controls,
            )
        for key in ["boil", "stop"]:
            add(
                key,
                "button",
                name=key.capitalize(),
                command_topic=f"{base}/set/{key}",
                payload_press="PRESS",
                icon="mdi:kettle-steam-outline" if key == "boil" else "mdi:stop",
                **controls,
            )
    add(
        "connection",
        "sensor",
        name="Connection",
        state_topic=f"{base}/diagnostic",
        value_template="{{ value_json.state }}",
        json_attributes_topic=f"{base}/diagnostic",
        availability=[{"topic": f"{base}/bridge"}],
        entity_category="diagnostic",
    )
    payload = {
        "device": {
            "identifiers": [f"haus_kettle_{device_id}"],
            "name": name,
            "manufacturer": "Xiaomi",
            "model": MODEL,
            "sw_version": __version__,
        },
        "origin": {
            "name": "Haus Kettle",
            "sw_version": __version__,
            "support_url": "https://github.com/resonaura/haus-kettle",
        },
        "components": components,
    }
    return f"homeassistant/device/haus_kettle_{device_id}/config", payload
