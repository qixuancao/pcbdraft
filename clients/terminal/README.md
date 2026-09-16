# PCBDraft Terminal

This is the TypeScript terminal client for PCBDraft. The loopback FastAPI API
remains authoritative for projects, sessions, jobs, cancellation, permissions,
and PCB revisions. This client does not read PCBDraft's SQLite state.

In one terminal, start the loopback-only Python service from the repository
root with:

```sh
uv run python -c 'from pcbdraft.interfaces.cli import main; raise SystemExit(main())' gui --host 127.0.0.1 --port 9130
```

Then start the terminal client:

```sh
cd clients/terminal
bun install
bun run dev
```

Use `/new <name>` to create and immediately open an empty synchronized PCB
project. `/resume` and `/open` restore an existing project. Slash commands
accept unique, case-insensitive prefixes, so `/n` and `/NEW` resolve to `/new`,
while ambiguous prefixes produce an error instead of guessing.

After a message is submitted, the client follows the GUI lifecycle SSE stream.
It shows the current job phase, then reloads the canonical session after a
terminal job event and prints the saved assistant reply. The lifecycle stream
does not contain token deltas, so this client does not present status events as
token streaming.
