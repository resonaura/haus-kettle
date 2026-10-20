# Haus Kettle

Local control for **Xiaomi Smart Kettle 2 Pro (`xiaomi.kettle.v21`)**, with one physical device in Home Assistant and [Haus](https://github.com/resonaura/haus). The `water_heater` entity exposes the measured temperature, a 40–99 °C target, Heat (`electric`), Boil (`performance`) and Off. Separate entities provide Boil/Stop buttons, keep-warm temperature/duration, reminders, quiet mode, lift/fault status and connection diagnostics.

## Install

Add `https://github.com/resonaura/haus-kettle` to **Settings → Apps → App store → Repositories**, then install **Haus Kettle**. On earlier Home Assistant releases these pages are called Add-ons. Images are built for `amd64` and `aarch64` after the tests pass.

With **Connection = home_assistant**, the app imports the local token through an already configured **Xiaomi Miot Auto** integration. One supported kettle is selected automatically; enter its Xiaomi device ID if there are several. The existing integration handles regional accounts, sign-in and two-factor verification. When a cloud session expires, cached local control continues. If a token needs renewal, reconnect the relevant account in Home Assistant; the bridge retries import with bounded backoff. No Xiaomi username/password or verification code is stored by this app.

Alternatively select **local**, supply the kettle's IP, device ID and local token. This path requires no Xiaomi account or cloud connection. Keep the IP stable with a DHCP reservation. Leave the MQTT host empty to use Home Assistant's broker automatically.

**Read-only commissioning** disables every write and omits command topics from discovery. Use it when checking a newly installed bridge or an empty kettle. Changing connection settings, restarting, broker reconnection and automatic recovery never turn on the kettle.

## Behavior

- Every activation requires a fresh local reading with no reported fault and the kettle on its base. This is not a water-level sensor: the owner must ensure that water is present.
- Boil selects the kettle's native boiling mode, then starts it. Heat selects its native heating mode. Existing keep-warm and knob presets remain intact. Off uses its native stop action.
- Writes are serialized, never retried automatically, never retained and never replayed after reconnect. Stale, duplicate and retained MQTT commands are discarded. A failed preset change prevents the following activation.
- Read errors retain the last reported values but mark the device unavailable. A separate diagnostic remains readable while the bridge is connected. Polling backs off to 60 seconds and recovers automatically; UDP handshakes and requests have fixed deadlines.
- Local tokens are cached with mode `0600` in app data and included in normal Home Assistant app backups. Tokens, packets and raw transport errors are not logged. No network port, host networking or privileged container access is required.
- HA HomeKit Bridge supports `water_heater`. Export the main entity for temperature/mode control; optional buttons and configuration entities can remain in Home Assistant. A real HomeKit pairing/physical heating check is still necessary for a particular firmware.

## Development

```sh
cd haus-kettle
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Tests cover command validation, fresh-state checks, actual encrypted UDP reads against a synthetic kettle, stop/boil protocol parameters, failed-write ordering, cached credentials, expired-session recovery, MQTT discovery, retained/stale command rejection, bounded queues and reconnects. They never contact or heat a real kettle.

Protocol references: [Xiaomi MIoT instance](https://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:kettle:0000A009:xiaomi-v21:1), [native mode descriptions](https://home.miot-spec.com/spec/xiaomi.kettle.v21), [HA MQTT water heater](https://www.home-assistant.io/integrations/water_heater.mqtt/), [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery), [python-miio](https://github.com/rytilahti/python-miio), [Xiaomi Miot Auto](https://github.com/al-one/hass-xiaomi-miot).

GPL-3.0-or-later. Xiaomi and Home Assistant are trademarks of their respective owners; this project is independent.
