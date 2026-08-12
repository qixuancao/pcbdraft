"""Public error types and stable CLI exit codes."""


class PcbAgentError(Exception):
    """An expected, already-sanitized runtime failure."""

    exit_code = 1


class ValidationError(PcbAgentError):
    """Unsafe or invalid user/agent supplied data."""

    exit_code = 2


class TransactionRejected(PcbAgentError):
    """A completed patch transaction that did not pass its gates."""

    exit_code = 3

    def __init__(self, message: str, run_dir: str) -> None:
        super().__init__(message)
        self.run_dir = run_dir
