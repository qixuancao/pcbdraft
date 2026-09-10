"""Retired upstream uninstaller; installation and user data remain untouched."""

import sys

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle

run_uninstall = unsupported_lifecycle
run_gui_uninstall = unsupported_lifecycle
remove_path_from_shell_configs = unsupported_lifecycle
remove_wrapper_script = unsupported_lifecycle
remove_node_symlinks = unsupported_lifecycle
uninstall_gateway_service = unsupported_lifecycle
remove_path_from_windows_registry = unsupported_lifecycle
remove_pcbdraft_env_vars_windows = unsupported_lifecycle
remove_portable_tooling_windows = unsupported_lifecycle
_perform_uninstall = unsupported_lifecycle


def main(argv=None) -> int:
    try:
        run_uninstall(argv)
    except PCBDraftError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
