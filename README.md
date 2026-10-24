# Haus Kettle

Local control for **Xiaomi Smart Kettle 2 Pro (`xiaomi.kettle.v21`)**, with one physical device in Home Assistant and [Haus](https://github.com/resonaura/haus). The `water_heater` entity exposes the measured temperature, a 40–99 °C target, Heat (`electric`), Boil (`performance`) and Off. Separate entities provide Boil/Stop buttons, keep-warm temperature/duration, reminders, quiet mode, lift/fault status and connection diagnostics.

## Install

Add `https://github.com/resonaura/haus-kettle` to **Settings → Apps → App store → Repositories**, then install **Haus Kettle**. On earlier Home Assistant releases these pages are called Add-ons. Images are built for `amd64` and `aarch64` after the tests pass.

With **Connection = home_assistant**, the app imports the local token through an already configured **Xiaomi Miot Auto** integration. One supported kettle is selected automatically; enter its Xiaomi device ID if there are several. The existing integration handles regional accounts, sign-in and two-factor verification. When a cloud session expires, cached local control continues. If a token needs renewal, reconnect the relevant account in Home Assistant; the bridge retries import with bounded backoff. No Xiaomi username/password or verification code is stored by this app.

Alternatively select **local**, supply the kettle's IP, device ID and local token. This path requires no Xiaomi account or cloud connection. Keep the IP stable with a DHCP reservation. Leave the MQTT host empty to use Home Assistant's broker automatically.

**Read-only commissioning** disables every device write and omits heating/settings command topics from discovery. The diagnostic **Refresh readings** button remains available because it only reads the kettle. Use commissioning when checking a newly installed bridge or an empty kettle. Changing connection settings, restarting, broker reconnection and automatic recovery never turn on the kettle.

## Behavior

- Every activation requires a fresh local reading with no reported fault and the kettle on its base. This is not a water-level sensor: the owner must ensure that water is present.
- Boil selects the kettle's native boiling mode, then starts it. Heat selects its native heating mode. Existing keep-warm and knob presets remain intact. Off cancels the task and clears the power latch; an idle kettle is reported as Off even if its firmware keeps that latch set.
- Writes are serialized, never retried automatically, never retained and never replayed after reconnect. Stale and retained MQTT commands are discarded. QoS 1 delivery is enabled; duplicate activations are rejected, while a redelivered Off remains safe and cancels queued activations. A failed preset change prevents the following activation.
- State is checked every second during an active task or pending command and every two seconds while idle, with an immediate reading after a command. **Refresh readings** bypasses connection backoff for one fresh local reading; Home Assistant's generic update-entity action cannot force an MQTT device to poll.
- A device acknowledgement and an observed result are tracked separately. Diagnostics correlate a sequence with its requested outcome and confirm it only when a fresh reading matches. Unmatched requests settle after 12 seconds; reconnects, superseding requests and expiry also settle explicitly. A lost acknowledgement can still be resolved by matching telemetry, without repeating a device write. The last actual state is always retained; private packets and credentials are never logged.
- Refresh requests have separate diagnostics and deadlines, so refreshing during a pending Heat, Boil or Off never replaces its expected result or clears a control error. Heater attributes expose `last_command` / `last_command_error` and `last_refresh` / `last_refresh_error` for independent UI confirmation.
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
