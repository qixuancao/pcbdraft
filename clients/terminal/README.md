# PCBDraft Terminal

This is the TypeScript terminal client for PCBDraft. The loopback FastAPI API
remains authoritative for projects, sessions, jobs, cancellation, permissions,
and PCB revisions. This client does not read PCBDraft's SQLite state.

From the repository root, run the official launcher:

```sh
uv run pcbdraft terminal
```

The launcher checks Bun, starts the loopback GUI API when needed, waits for it
to become healthy, and then runs this client. If a healthy PCBDraft GUI already
uses port 9130, the launcher reuses it and leaves it running on exit. To require
an existing service or choose another port, use:

```sh
uv run pcbdraft terminal --no-start-gui
uv run pcbdraft terminal --port 9131
```

Use `/new <name>` to create and immediately open an empty synchronized PCB
project. `/resume` and `/open` restore an existing project. Slash commands
accept unique, case-insensitive prefixes, so `/n` and `/NEW` resolve to `/new`,
while ambiguous prefixes produce an error instead of guessing.

After a message is submitted, the client follows the GUI SSE stream and renders
bounded `assistant.delta` events for that turn as a live preview. On completion,
cancellation, or a resumed connection, it reloads the canonical session. The
saved assistant message remains authoritative and is not printed twice when it
matches the completed preview.
