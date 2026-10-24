# 0.1.4

- Confirm a change only when fresh local readings match its expected result, with separate device acknowledgement and observation diagnostics.
- Finish unresolved confirmation after a bounded wait, disconnect, expiry or superseding request. A missing acknowledgement never repeats a write; later matching readings can still confirm it.
- Add a diagnostic **Refresh readings** button that forces a local read even during connection backoff. It remains available during read-only commissioning and when the kettle is offline.
- Keep refresh and control confirmation separate so refreshing never clears a pending Heat, Boil or Off result or its error.
- Publish correlated command/refresh results in heater attributes, retaining the actual measured state and last successful reading time on failures.

Startup, reconnects, refresh and automatic recovery never start heating. No heating command was sent during development; verification uses synthetic devices and encrypted loopback transport tests.
