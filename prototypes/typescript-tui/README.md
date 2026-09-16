# TypeScript TUI prototype

This is a TypeScript terminal client for the existing local PCBDraft GUI API.
The FastAPI GUI adapter remains authoritative for project discovery, persisted
turns, jobs, cancellation, permissions, and PCB revisions.

In one terminal, start the loopback-only Python service from the repository
root with:

```sh
uv run python -c 'from pcbdraft.interfaces.cli import main; raise SystemExit(main())' gui --host 127.0.0.1 --port 9130
```

In this directory, run `bun install` then `bun run dev`.

Try `/resume`, then `/open 1`. Plain text queues a model turn and `/stop`
requests cancellation through the canonical job boundary. The first prototype
refreshes completed messages through `/open`; incremental assistant text and
full legacy-terminal history are deliberately separate follow-up work.
