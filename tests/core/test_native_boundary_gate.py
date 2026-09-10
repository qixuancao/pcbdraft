"""Reject executable legacy runtime dependencies, not historical/model text."""

from __future__ import annotations

import ast
import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core import legacy_migration
from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.runtime_paths import runtime_home

_LEGACY_MODULES = {"hermes_cli", "hermes_constants", "hermes_state", "run_agent"}
_LEGACY_ENV = re.compile(r"(?:HERMES_[A-Z0-9_]+|PCBDRAFT_HERMES_[A-Z0-9_]+)\Z")
_LEGACY_COMMAND = re.compile(
    r"(?:^|[\s/;|&])hermes(?:[\s;|&]|$)"
    r"|(?:^|\s)-m\s+(?:hermes_cli|run_agent)(?:[.\s]|$)"
)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _strings(node: ast.AST):
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def _rejection_only_read(path: Path, tree: ast.Module) -> ast.Call | None:
    """Allow only the leading fail-closed guard, never an entire function."""
    if path.as_posix() != "core/legacy_migration.py":
        return None
    expected = ast.parse(
        'os.environ.get("PCBDRAFT_HERMES_HOME", "").strip()', mode="eval"
    ).body
    for function in tree.body:
        if not (
            isinstance(function, ast.FunctionDef)
            and function.name == "migrate_legacy_runtime_home"
        ):
            continue
        body = function.body
        if ast.get_docstring(function) is not None:
            body = body[1:]
        if not body or not isinstance(body[0], ast.If):
            return None
        guard = body[0]
        if (
            ast.dump(guard.test) != ast.dump(expected)
            or guard.orelse
            or len(guard.body) != 1
            or not isinstance(guard.body[0], ast.Raise)
        ):
            return None
        error = guard.body[0].exc
        if not isinstance(error, ast.Call) or _name(error.func) != "PCBDraftError":
            return None
        return next(
            node
            for node in ast.walk(guard.test)
            if isinstance(node, ast.Call) and _name(node.func) == "os.environ.get"
        )
    return None


class NativeBoundaryGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2] / "src" / "pcbdraft"
        cls.sources = [
            (path.relative_to(root), ast.parse(path.read_text(encoding="utf-8")))
            for path in sorted(root.rglob("*.py"))
        ]

    def test_native_imports_do_not_require_legacy_modules(self) -> None:
        violations = []
        for path, tree in self.sources:
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                elif (
                    isinstance(node, ast.Call)
                    and _name(node.func).split(".")[-1]
                    in {"import_module", "__import__"}
                    and node.args
                ):
                    names = list(_strings(node.args[0]))
                for name in names:
                    if name.split(".")[0] in _LEGACY_MODULES:
                        violations.append(f"{path}:{node.lineno}: {name}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_native_runtime_does_not_read_legacy_environment(self) -> None:
        violations = []
        rejection_reads = 0
        for path, tree in self.sources:
            allowed_read = _rejection_only_read(path, tree)
            for node in ast.walk(tree):
                key = None
                if isinstance(node, ast.Call) and node.args:
                    name = _name(node.func)
                    if name in {"os.environ.get", "environ.get"} or name.split(".")[
                        -1
                    ] in {"getenv", "_getenv", "get_secret", "_get_secret"}:
                        key = node.args[0]
                elif (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.ctx, ast.Load)
                    and _name(node.value) in {"os.environ", "environ"}
                ):
                    key = node.slice
                if key is not None:
                    for value in _strings(key):
                        if _LEGACY_ENV.fullmatch(value):
                            if node is allowed_read and value == "PCBDRAFT_HERMES_HOME":
                                rejection_reads += 1
                                continue
                            violations.append(f"{path}:{node.lineno}: {value}")
        self.assertEqual(violations, [], "\n".join(violations))
        self.assertEqual(rejection_reads, 1, "expected one explicit rejection guard")

    def test_native_execution_does_not_launch_legacy_commands(self) -> None:
        violations = []
        launchers = {
            "run",
            "Popen",
            "call",
            "check_call",
            "check_output",
            "run_command",
            "create_subprocess_exec",
            "create_subprocess_shell",
            "system",
            "popen",
            "execv",
            "execve",
            "execvp",
            "execvpe",
        }
        for path, tree in self.sources:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _name(node.func).split(".")[-1] not in launchers:
                    continue
                arguments = list(node.args[:1]) + [
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in {"args", "command", "argv", "executable"}
                ]
                # asyncio's exec API takes the command as positional varargs.
                if _name(node.func).endswith("create_subprocess_exec"):
                    arguments = list(node.args)
                for argument in arguments:
                    command = " ".join(_strings(argument))
                    if _LEGACY_COMMAND.search(command):
                        violations.append(f"{path}:{node.lineno}: {command}")
        self.assertEqual(violations, [], "\n".join(violations))


class LegacyOverrideBoundaryTests(unittest.TestCase):
    def test_pure_getter_never_reads_deprecated_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for explicit in ("", str(root / "explicit")):
                with (
                    self.subTest(explicit=explicit),
                    patch.dict(
                        os.environ,
                        {
                            "PCBDRAFT_CONFIG": str(root / "config.json"),
                            "PCBDRAFT_RUNTIME_HOME": explicit,
                            "PCBDRAFT_HERMES_HOME": str(root / "old"),
                            "HERMES_HOME": str(root / "standalone"),
                        },
                        clear=True,
                    ),
                ):
                    original_getitem = type(os.environ).__getitem__

                    def guarded_getitem(environment, key, _getitem=original_getitem):
                        self.assertNotIn(key, {"PCBDRAFT_HERMES_HOME", "HERMES_HOME"})
                        return _getitem(environment, key)

                    with patch.object(type(os.environ), "__getitem__", guarded_getitem):
                        self.assertEqual(
                            runtime_home(),
                            Path(explicit) if explicit else root / "runtime",
                        )
            self.assertEqual(list(root.iterdir()), [])

    def test_deprecated_override_raises_before_any_directory_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for explicit in ("", str(root / "explicit")):
                with (
                    self.subTest(explicit=explicit),
                    patch.dict(
                        os.environ,
                        {
                            "PCBDRAFT_CONFIG": str(root / "config.json"),
                            "PCBDRAFT_RUNTIME_HOME": explicit,
                            "PCBDRAFT_HERMES_HOME": str(root / "old"),
                        },
                        clear=True,
                    ),
                    ExitStack() as guards,
                ):
                    for target, names in (
                        (
                            Path,
                            (
                                "stat",
                                "lstat",
                                "exists",
                                "is_dir",
                                "is_symlink",
                                "mkdir",
                                "rename",
                                "replace",
                                "open",
                                "iterdir",
                            ),
                        ),
                        (
                            os,
                            ("scandir", "stat", "lstat", "mkdir", "rename", "replace"),
                        ),
                        (
                            legacy_migration,
                            (
                                "pcbdraft_config_dir",
                                "ResourceLock",
                                "atomic_write_json",
                            ),
                        ),
                    ):
                        for name in names:
                            guards.enter_context(
                                patch.object(
                                    target,
                                    name,
                                    side_effect=AssertionError(
                                        f"unexpected directory operation: {name}"
                                    ),
                                )
                            )
                    with self.assertRaisesRegex(
                        PCBDraftError, "Set PCBDRAFT_RUNTIME_HOME"
                    ) as raised:
                        legacy_migration.migrate_legacy_runtime_home()
                    self.assertIn("remove PCBDRAFT_HERMES_HOME", str(raised.exception))
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
