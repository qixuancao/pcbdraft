"""Retired gateway-supervisor boot hook.

Old gateway state, PID files and supervision directories are retained as data;
they do not authorize service registration, process takeover or cleanup.
"""

import sys

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle

reconcile_profile_gateways = unsupported_lifecycle


def main() -> int:
    try:
        reconcile_profile_gateways()
    except PCBDraftError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
