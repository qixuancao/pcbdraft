"""User-facing CLI and interactive terminal adapters.

The ``pcbdraft`` CLI is the only public entry point.  Subcommands
(``doctor``, ``setup``, ``repository``, ``trace``) run directly; a bare
launch starts the interactive Hermes-based terminal owned by
:mod:`pcbdraft.interfaces.hermes_cli`, with the PCBDraft slash-command
surface in :mod:`pcbdraft.interfaces.commands`.
"""
