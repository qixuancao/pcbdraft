"""Retired remote desktop backend protocol.

The public PCBDraft GUI has no SSH token/nonce backend. Old lockfiles and tokens
are retained without reading secrets, spawning a backend, or terminating PIDs.
"""

import json
import sys
from typing import Any

from pcbdraft.core.runtime_environment import get_default_runtime_root
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle


def inspect_pcbdraft(pcbdraft_path: str) -> dict[str, Any]:
    return {
        "path": pcbdraft_path,
        "supported": False,
        "detail": "PCBDraft remote desktop lifecycle is unsupported.",
    }


spawn_backend = unsupported_lifecycle
terminate_owned = unsupported_lifecycle
process_state = unsupported_lifecycle
upload_token = unsupported_lifecycle
read_token = unsupported_lifecycle
read_lock = unsupported_lifecycle
write_lock = unsupported_lifecycle
remove_artifact = unsupported_lifecycle


def dispatch(argv: list[str]) -> Any:
    if argv == ["probe"]:
        return {
            "supported": False,
            "pcbdraftHome": str(get_default_runtime_root()),
            "python": sys.executable,
        }
    if len(argv) == 2 and argv[0] == "inspect":
        return inspect_pcbdraft(argv[1])
    return unsupported_lifecycle()


def main() -> None:
    try:
        print(json.dumps(dispatch(sys.argv[1:])))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
