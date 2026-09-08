"""Random PCBDraft tips shown at terminal-session start."""

import random

# Keep these tips tied to the closed slash-command registry. A tip must describe
# something the PCBDraft terminal actually accepts; standalone upstream product
# commands and services do not belong on this surface.
TIPS = [
    "Describe the board, constraints, and intended components in plain language to start a PCB draft.",
    "/new <name> creates a PCB project in the current project repository.",
    "/projects lists the PCB projects in the current repository.",
    "/project [directory] shows or switches the PCB project repository.",
    "/open <id> selects an existing PCB project.",
    "/connect opens model-provider setup or reauthentication.",
    "/model shows or switches the saved model selection.",
    "/review [id] summarizes the selected PCB project's current state.",
    "/validate [id] checks the selected PCB project.",
    "/confirm [id] approves the current candidate for generation.",
    "/discard [id] discards the staged semantic change.",
    "/logs [id] shows recent events for the selected PCB project.",
    "/release [id] creates release outputs for the selected PCB project.",
    "/status shows the active session, model, tokens, and context usage.",
    "/goal <text> gives PCBDraft a standing objective to work on across turns.",
    "/retry resends your last message to the agent.",
    "/undo [N] backs up one or more user turns and re-prompts.",
    "/stop interrupts the current turn and stops background processes.",
    "/clear clears the terminal and starts a new session.",
    "/help lists every slash command available in this PCBDraft terminal.",
    "/quit exits PCBDraft; use /quit --delete to remove the session history too.",
    "Alt+Enter or Ctrl+J inserts a newline for multi-line input.",
    "Tab completes slash commands and accepts suggested input.",
    "Ctrl+C interrupts the current turn; press it again promptly to exit.",
]


def get_random_tip(exclude_recent: int = 0) -> str:
    """Return one tip; ``exclude_recent`` is reserved for future deduplication."""
    del exclude_recent
    return random.choice(TIPS)  # noqa: S311 - discovery order need not be secure
