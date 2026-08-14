"""Public error types and stable CLI exit codes."""


class PCBDraftError(Exception):
    """An expected, already-sanitized runtime failure."""

    exit_code = 1


class ValidationError(PCBDraftError):
    """Unsafe or invalid user/agent supplied data."""

    exit_code = 2


class TransactionRejected(PCBDraftError):
    """A completed patch transaction that did not pass its gates."""

    exit_code = 3

    def __init__(self, message: str, run_dir: str) -> None:
        super().__init__(message)
        self.run_dir = run_dir
