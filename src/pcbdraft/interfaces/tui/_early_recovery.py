"""Import-safe retirement of automatic source-install recovery.

Old interrupted-update markers are inert and retained. Importing a UI helper
must never download dependencies or replace the interpreter environment.
"""

LAZY_REFRESH_IMPORT_PROBES: tuple[tuple[str, str], ...] = ()
LAZY_REFRESH_REPAIR_PACKAGES: dict[str, str] = {}


def _should_skip_external_secret_sources() -> bool:
    return False


def recover_if_needed(*_args, **_kwargs) -> bool:
    return False
