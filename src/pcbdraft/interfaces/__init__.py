"""User-facing CLI and interactive terminal adapters.

The ``pcbdraft`` CLI is the only public entry point.  Subcommands
(``doctor``, ``setup``, ``repository``, ``trace``) run directly; a bare
launch starts the interactive native terminal owned by
:mod:`pcbdraft.interfaces.terminal`, with the PCBDraft slash-command
surface in :mod:`pcbdraft.interfaces.tui.project_commands`.
"""
