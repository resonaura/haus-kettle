import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from .bridge import Bridge
from .credentials import CredentialsError, load, mqtt_options, refresh
from .transport import Transport


def main():
    # Library debug output can contain tokens and packets. Log only fixed application messages.
    logging.getLogger("miio").setLevel(logging.CRITICAL)
    options = json.loads(Path(os.environ.get("HAUS_OPTIONS", "/data/options.json")).read_text())
    cache = Path(os.environ.get("HAUS_CREDENTIALS", "/data/credentials.json"))
    stop = threading.Event()
    for kind in (signal.SIGINT, signal.SIGTERM):
        signal.signal(kind, lambda *_args: stop.set())
    while not stop.is_set():
        try:
            credentials = load(options, cache)
            broker = mqtt_options(options)
            break
        except CredentialsError as error:
            print(str(error), flush=True)
            stop.wait(60)
    if stop.is_set():
        return
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"haus-kettle-{credentials['did']}",
        clean_session=True,
    )
    client.username_pw_set(broker.get("username"), broker.get("password"))
    if broker.get("ssl"):
        client.tls_set()
    bridge = Bridge(client, Transport(credentials), options)
    client.on_connect, client.on_disconnect, client.on_message = (
        bridge.on_connect,
        bridge.on_disconnect,
        bridge.on_message,
    )
    client.will_set(f"{bridge.base}/bridge", "offline", qos=1, retain=True)
    client.reconnect_delay_set(2, 60)
    client.max_queued_messages_set(32)
    client.connect_async(broker["host"], int(broker.get("port", 1883)), 30)
    client.loop_start()
    last_refresh = time.monotonic() - 900
    print(
        "Haus Kettle started. Device writes only follow fresh explicit commands.",
        flush=True,
    )
    try:
        while not stop.wait(0.1):
            bridge.tick()
            if bridge.failures >= 3 and time.monotonic() - last_refresh > 900:
                last_refresh = time.monotonic()
                try:
                    renewed = refresh(options, cache, renew=True)
                    if str(renewed["did"]) != str(credentials["did"]):
                        raise CredentialsError(
                            "The selected kettle changed. Check the device ID in configuration."
                        )
                    bridge.transport = Transport(renewed)
                    bridge.next_poll = 0
                except CredentialsError as error:
                    bridge.diagnostic("Check connection", str(error))
    finally:
        bridge.close()
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"Haus Kettle could not start ({type(error).__name__}). Check its configuration and broker connection.",
            flush=True,
        )
        raise SystemExit(1) from None
