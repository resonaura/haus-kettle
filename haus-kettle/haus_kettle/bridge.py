"""Serial polling/commands, bounded queues, reconnect-safe discovery and availability."""

import json
import queue
import time

from .discovery import discovery
from .model import CommandError, plan


class Bridge:
    def __init__(self, client, transport, options, *, clock=time.monotonic):
        self.client, self.transport, self.options, self.clock = (
            client,
            transport,
            options,
            clock,
        )
        self.base = f"haus/kettle/{transport.did}"
        self.commands = queue.Queue(maxsize=8)
        self.state = {}
        self.last_read = None
        self.failures = 0
        self.connected = False
        self.rediscover = False
        self.next_poll = 0
        self.read_only = bool(options.get("read_only", False))
        self.command_error = ""
        self.stop_generation = 0
        self.command_sequence = 0
        self.last_command = None

    def log(self, event, **fields):
        # Fixed field names and validated commands only. Never log MQTT packets or credentials.
        print(json.dumps({"event": event, **fields}, separators=(",", ":")), flush=True)

    def publish(self, suffix, value, retain=True):
        body = json.dumps(value, separators=(",", ":")) if isinstance(value, dict) else value
        self.client.publish(f"{self.base}/{suffix}", body, qos=1, retain=retain)

    def diagnostic(self, state, detail=""):
        self.publish(
            "diagnostic",
            {
                "state": state,
                "detail": detail,
                "read_only": self.read_only,
                "last_command_error": self.command_error,
                "last_command": self.last_command,
            },
        )

    def on_connect(self, _client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            return
        self.connected = True
        self.client.subscribe([(f"{self.base}/set/+", 1), ("homeassistant/status", 0)])
        self.publish("bridge", "online")
        self.publish("availability", "offline")
        self.rediscover = True
        self.next_poll = 0

    def on_disconnect(self, *_args):
        self.connected = False
        self.stop_generation += 1
        self._clear_commands()

    def _clear_commands(self):
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                return

    def on_message(self, _client, _userdata, message):
        if message.topic == "homeassistant/status":
            if message.payload == b"online":
                self.rediscover = True
            return
        # Retained/re-delivered commands must never restart a kettle after reconnect.
        if not self.connected or message.retain or len(message.payload) > 128:
            return
        prefix = f"{self.base}/set/"
        if not message.topic.startswith(prefix):
            return
        try:
            command = (
                self.clock(),
                message.topic[len(prefix) :],
                message.payload.decode("utf-8"),
            )
            stopping = command[1:] in {("stop", "PRESS"), ("mode", "off"), ("power", "OFF")}
            if message.dup and not stopping:
                return
            if stopping:
                self.stop_generation += 1
                self._clear_commands()
            self.commands.put_nowait(command)
        except (UnicodeDecodeError, queue.Full):
            return

    def tick(self):
        if not self.connected:
            return
        if self.rediscover:
            self.rediscover = False
            topic, payload = discovery(
                self.transport.did,
                self.options.get("name", "Kitchen Kettle"),
                read_only=self.read_only,
            )
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        now = self.clock()
        try:
            created, key, value = self.commands.get_nowait()
        except queue.Empty:
            pass
        else:
            stopping = key == "stop" or (key in {"mode", "power"} and value in {"off", "OFF"})
            generation = self.stop_generation
            if now - created <= (30 if stopping else 4):
                try:
                    # Re-read first: never activate from a cached/stale safety state.
                    if not stopping:
                        self.read()
                    operations = plan(
                        key,
                        value,
                        self.state,
                        read_only=self.read_only,
                        age=self.clock() - self.last_read
                        if self.last_read is not None
                        else float("inf"),
                    )
                    self.command_sequence += 1
                    self.last_command = {
                        "sequence": self.command_sequence,
                        "control": key,
                        "status": "sending",
                    }
                    self.log(
                        "command", sequence=self.command_sequence, control=key, stopping=stopping
                    )
                    self.transport.write(
                        operations,
                        cancelled=lambda: not self.connected or generation != self.stop_generation,
                    )
                    self.command_error = ""
                    self.last_command["status"] = "accepted"
                    self.next_poll = 0
                except CommandError as error:
                    self.command_error = str(error)
                    if self.last_command:
                        self.last_command["status"] = "rejected"
                    self.log("command_rejected", reason=str(error))
                    self.diagnostic("Check request", str(error))
                except Exception as error:
                    self.command_error = (
                        "The command could not be confirmed. It will not be repeated."
                    )
                    if self.last_command:
                        self.last_command["status"] = "unconfirmed"
                    self.log("command_unconfirmed", reason=type(error).__name__)
                    self.diagnostic(
                        "Check connection",
                        "The command could not be confirmed. It will not be repeated.",
                    )
                    self.next_poll = 0
        if self.clock() >= self.next_poll:
            try:
                self.read()
                self.next_poll = self.clock() + (1 if self.state.get("mode") != "off" else 2)
            except Exception:
                self.failures += 1
                self.publish("availability", "offline")
                self.diagnostic(
                    "Offline",
                    "Check power and the kettle's local connection. Previous readings are retained.",
                )
                self.next_poll = self.clock() + min(60, 5 * 2 ** min(self.failures - 1, 4))

    def read(self):
        self.state = self.transport.read()
        if self.last_command and self.last_command["status"] == "accepted":
            self.last_command.update(
                status="observed", mode=self.state["mode"], activity=self.state["activity"]
            )
            self.log("command_state", **self.last_command, power=self.state["power"])
        self.last_read = self.clock()
        self.failures = 0
        self.publish("state", self.state)
        self.publish(
            "attributes",
            {
                "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "read_only": self.read_only,
                "source": "local",
                "activity": self.state["activity"],
                "last_command": self.last_command,
            },
        )
        self.publish("availability", "online")
        self.diagnostic("Connected")

    def close(self):
        self.connected = False
        self._clear_commands()
        self.publish("availability", "offline")
        self.publish("bridge", "offline")
