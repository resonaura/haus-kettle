"""Serial polling/commands, bounded queues, reconnect-safe discovery and availability."""

import json
import queue
import time

from .discovery import discovery
from .model import PROPERTIES, CommandError, expected_state, plan

CONFIRMATION_SECONDS = 12


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
        self.last_seen = None
        self.failures = 0
        self.connected = False
        self.rediscover = False
        self.next_poll = 0
        self.read_only = bool(options.get("read_only", False))
        self.command_error = ""
        self.stop_generation = 0
        self.command_sequence = 0
        self.last_command = None
        self.confirm_by = None
        self.last_refresh = None
        self.refresh_by = None
        self.refresh_error = ""

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
                "last_refresh": self.last_refresh,
                "last_refresh_error": self.refresh_error,
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
        if self.confirm_by is not None:
            self._unconfirmed("The connection changed before the command could be confirmed.")
        if self.refresh_by is not None:
            self._refresh_unconfirmed("The connection changed before readings could be refreshed.")

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
            if key == "refresh":
                self._refresh_request(created, value, now)
            else:
                self._control_request(created, key, value, now)
        if not self.connected:
            return
        if self.clock() >= self.next_poll:
            try:
                self.read()
                self.next_poll = self.clock() + (
                    1 if self.confirm_by is not None or self.state.get("mode") != "off" else 2
                )
            except Exception:
                self.failures += 1
                self.publish("availability", "offline")
                self.diagnostic(
                    "Offline",
                    "Check power and the kettle's local connection. Previous readings are retained.",
                )
                self.next_poll = self.clock() + min(60, 5 * 2 ** min(self.failures - 1, 4))
        if self.confirm_by is not None and self.clock() >= self.confirm_by:
            self._unconfirmed(
                "The kettle has not reported the requested state. It will not be repeated."
            )
        if self.refresh_by is not None and self.clock() >= self.refresh_by:
            self._refresh_unconfirmed(
                "The kettle did not return fresh readings before the deadline."
            )

    def _refresh_request(self, created, value, now):
        # Reading telemetry must never supersede or confirm an unrelated device write.
        self.command_sequence += 1
        self.last_refresh = {
            "sequence": self.command_sequence,
            "control": "refresh",
            "status": "accepted",
        }
        self.refresh_by = None
        self.refresh_error = ""
        if now - created > 4:
            self.last_refresh["status"] = "expired"
            self.refresh_error = "The refresh request expired before it could be sent. Try again."
        elif value != "PRESS":
            self.last_refresh["status"] = "rejected"
            self.refresh_error = "Unsupported refresh request."
        else:
            self.refresh_by = self.clock() + CONFIRMATION_SECONDS
            self.next_poll = 0
        self.log("refresh_request", **self.last_refresh)
        self.publish_attributes()
        if self.refresh_error:
            self.diagnostic("Check request", self.refresh_error)

    def _control_request(self, created, key, value, now):
        stopping = key == "stop" or (key in {"mode", "power"} and value in {"off", "OFF"})
        generation = self.stop_generation
        if self.confirm_by is not None:
            self.last_command["status"] = "superseded"
            self.log("command_superseded", **self.last_command)
        self.command_sequence += 1
        self.last_command = {
            "sequence": self.command_sequence,
            "control": key
            if key in PROPERTIES or key in {"stop", "boil", "mode"}
            else "unsupported",
            "status": "validating",
        }
        self.confirm_by = None
        if now - created <= (30 if stopping else 4):
            write_started = None
            operation_count = 0
            accepted_operations = 0
            operation_in_progress = None
            try:
                # Never activate from a cached/stale safety state.
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
                self.last_command.update(status="sending", expected=expected_state(key, value))
                self.log("command", **self.last_command, stopping=stopping)
                write_started = self.clock()
                command_sequence = self.last_command["sequence"]
                operation_count = len(operations)
                self.log(
                    "command_write_started",
                    sequence=command_sequence,
                    operation_count=operation_count,
                )

                def safe_targets(operation):
                    targets = []
                    for parameter in operation.params:
                        if not isinstance(parameter, dict):
                            continue
                        # MIoT property/action identifiers are useful for debugging;
                        # never include device IDs, values, tokens or packet contents.
                        target = {
                            key: parameter[key]
                            for key in ("siid", "piid", "aiid")
                            if isinstance(parameter.get(key), int)
                            and not isinstance(parameter.get(key), bool)
                        }
                        if target:
                            targets.append(target)
                    return targets

                def operation_started(operation, index, total):
                    nonlocal operation_in_progress
                    operation_in_progress = index + 1
                    self.log(
                        "command_operation_started",
                        sequence=command_sequence,
                        operation=operation_in_progress,
                        operation_count=total,
                        method=operation.method,
                        targets=safe_targets(operation),
                        elapsed_ms=max(0, round((self.clock() - write_started) * 1000)),
                    )

                def operation_accepted(operation, index, total):
                    nonlocal accepted_operations, operation_in_progress
                    accepted_operations = index + 1
                    operation_in_progress = None
                    self.log(
                        "command_operation_accepted",
                        sequence=command_sequence,
                        operation=index + 1,
                        operation_count=total,
                        method=operation.method,
                        targets=safe_targets(operation),
                        elapsed_ms=max(0, round((self.clock() - write_started) * 1000)),
                    )

                self.transport.write(
                    operations,
                    cancelled=lambda: not self.connected or generation != self.stop_generation,
                    on_operation=operation_accepted,
                    on_operation_start=operation_started,
                )
                self.command_error = ""
                if not self.connected or generation != self.stop_generation:
                    self._unconfirmed("The connection or request changed before confirmation.")
                else:
                    self.last_command["status"] = "accepted"
                    self.last_command["acknowledgement"] = "accepted"
                    self.confirm_by = self.clock() + CONFIRMATION_SECONDS
                self.next_poll = 0
            except CommandError as error:
                self.command_error = str(error)
                if self.last_command:
                    self.last_command["status"] = "rejected"
                self.log("command_rejected", **self.last_command, reason=str(error))
                if write_started is not None:
                    self.log(
                        "command_write_interrupted",
                        sequence=command_sequence,
                        operation_count=operation_count,
                        accepted_operation_count=accepted_operations,
                        operation_in_progress=operation_in_progress,
                        next_operation=accepted_operations + 1
                        if operation_in_progress is None and accepted_operations < operation_count
                        else None,
                        phase="operation"
                        if operation_in_progress is not None
                        else "handshake"
                        if accepted_operations == 0
                        else "between_operations",
                        error_type=type(error).__name__,
                        elapsed_ms=max(0, round((self.clock() - write_started) * 1000)),
                    )
                self.publish_attributes()
                self.diagnostic("Check request", str(error))
            except Exception as error:
                self.command_error = "The command could not be confirmed. It will not be repeated."
                if self.last_command:
                    self.last_command["status"] = "unconfirmed"
                    self.last_command["acknowledgement"] = "unconfirmed"
                    if "expected" in self.last_command and self.connected:
                        self.last_command["status"] = "waiting"
                        self.confirm_by = self.clock() + CONFIRMATION_SECONDS
                self.log("command_unconfirmed", **self.last_command, reason=type(error).__name__)
                if write_started is not None:
                    self.log(
                        "command_write_interrupted",
                        sequence=command_sequence,
                        operation_count=operation_count,
                        accepted_operation_count=accepted_operations,
                        operation_in_progress=operation_in_progress,
                        next_operation=accepted_operations + 1
                        if operation_in_progress is None and accepted_operations < operation_count
                        else None,
                        phase="operation"
                        if operation_in_progress is not None
                        else "handshake"
                        if accepted_operations == 0
                        else "between_operations",
                        error_type=type(error).__name__,
                        elapsed_ms=max(0, round((self.clock() - write_started) * 1000)),
                    )
                self.publish_attributes()
                self.diagnostic(
                    "Check connection",
                    "The command could not be confirmed. It will not be repeated.",
                )
                self.next_poll = 0
        else:
            self.last_command["status"] = "expired"
            self.command_error = "The request expired before it could be sent. Try again."
            self.log("command_expired", **self.last_command)
            self.publish_attributes()
            self.diagnostic("Check request", self.command_error)

    def read(self):
        self.state = self.transport.read()
        if self.refresh_by is not None:
            self.last_refresh["status"] = "observed"
            self.refresh_by = None
            self.refresh_error = ""
            self.log("refresh_state", **self.last_refresh)
        if self.confirm_by is not None:
            if all(
                self.state.get(key) == value for key, value in self.last_command["expected"].items()
            ):
                self.last_command.update(
                    status="observed", mode=self.state["mode"], activity=self.state["activity"]
                )
                self.confirm_by = None
                self.command_error = ""
                self.log("command_state", **self.last_command, power=self.state["power"])
            else:
                self.last_command["status"] = "waiting"
        self.last_read = self.clock()
        self.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.failures = 0
        if not self.connected:
            return
        self.publish("state", self.state)
        self.publish_attributes()
        self.publish("availability", "online")
        self.diagnostic("Connected")

    def publish_attributes(self):
        self.publish(
            "attributes",
            {
                "last_seen": self.last_seen,
                "read_only": self.read_only,
                "source": "local",
                "activity": self.state.get("activity"),
                "last_command": self.last_command,
                "last_command_error": self.command_error,
                "last_refresh": self.last_refresh,
                "last_refresh_error": self.refresh_error,
            },
        )

    def _unconfirmed(self, detail):
        self.confirm_by = None
        self.command_error = detail
        self.last_command["status"] = "unconfirmed"
        self.log("command_unconfirmed", **self.last_command)
        if self.connected:
            self.publish_attributes()
            self.diagnostic("Check request", detail)

    def _refresh_unconfirmed(self, detail):
        self.refresh_by = None
        self.refresh_error = detail
        self.last_refresh["status"] = "unconfirmed"
        self.log("refresh_unconfirmed", **self.last_refresh)
        if self.connected:
            self.publish_attributes()
            self.diagnostic("Check request", detail)

    def close(self):
        if self.confirm_by is not None:
            self._unconfirmed("The bridge stopped before the command could be confirmed.")
        if self.refresh_by is not None:
            self._refresh_unconfirmed("The bridge stopped before readings could be refreshed.")
        self.connected = False
        self._clear_commands()
        self.publish("availability", "offline")
        self.publish("bridge", "offline")
