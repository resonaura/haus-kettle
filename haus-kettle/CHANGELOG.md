# 0.1.5

- Log each MIoT write step as it starts and after it is acknowledged, with command sequence, operation order, method, property/action identifiers and elapsed time. Interrupted writes identify whether the bridge was handshaking, waiting on a specific operation or between operations, so an Off request shows whether the stop action, power-off property or later state confirmation failed.
- Diagnostics never include device IDs, property values, tokens, encrypted packets or raw exceptions. Write progress callbacks run only after the device acknowledges each step; no command retry or replay was added.

No live activation command was sent during development; verification uses the synthetic encrypted UDP device.

# 0.1.4

- Confirm a change only when fresh local readings match its expected result, with separate device acknowledgement and observation diagnostics.
- Finish unresolved confirmation after a bounded wait, disconnect, expiry or superseding request. A missing acknowledgement never repeats a write; later matching readings can still confirm it.
- Add a diagnostic **Refresh readings** button that forces a local read even during connection backoff. It remains available during read-only commissioning and when the kettle is offline.
- Keep refresh and control confirmation separate so refreshing never clears a pending Heat, Boil or Off result or its error.
- Publish correlated command/refresh results in heater attributes, retaining the actual measured state and last successful reading time on failures.

Startup, reconnects, refresh and automatic recovery never start heating. No heating command was sent during development; verification uses synthetic devices and encrypted loopback transport tests.
