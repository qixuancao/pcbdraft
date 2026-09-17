from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.core.io import atomic_write_json, load_json_limited
from pcbdraft.services import application, application_tool_inspection
from pcbdraft.services.application import ApplicationService
from pcbdraft.services.application_tool_inspection import (
    ApplicationToolInspectionMixin,
)


class ApplicationToolInspectionTests(unittest.TestCase):
    def test_mixin_has_no_reverse_import_and_owns_only_inspection_methods(self):
        source = Path(application_tool_inspection.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("pcbdraft.services.application", imports)

        for name in (
            "_with_tool_result",
            "_inspect_pcb_tool",
            "_inspect_transaction_artifact",
            "_retained_evidence",
            "_inspect_library_tool",
            "inspect_installed_library",
            "_inspect_part_tool",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(ApplicationService, name),
                    getattr(ApplicationToolInspectionMixin, name),
                )

    def test_transaction_inspection_uses_legacy_reader_and_dynamic_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transaction_id = "20260917T120000Z-1234abcd"
            receipt_path = root / "transactions" / transaction_id / "receipt.json"
            receipt_path.parent.mkdir(parents=True)
            atomic_write_json(
                receipt_path,
                {
                    "schema": "pcbdraft-flat-operation-receipt",
                    "version": 2,
                    "status": "failed",
                    "operation": "route_net",
                    "postconditions": [
                        {"name": "first", "passed": True},
                        {"name": "second", "passed": False},
                    ],
                    "artifact": {"native_delta": "native-delta.json"},
                },
            )

            with (
                patch.object(
                    application,
                    "load_json_limited",
                    wraps=load_json_limited,
                ) as loader,
                patch.object(application, "TRANSACTION_INSPECTION_ITEM_LIMIT", 1),
            ):
                result = ApplicationService._inspect_transaction_artifact(
                    SimpleNamespace(root=root),
                    f"transaction:{transaction_id}",
                )

            loader.assert_called_once_with(
                receipt_path,
                application.TRANSACTION_INSPECTION_FILE_LIMIT,
            )
            self.assertEqual(
                result["detail"]["postconditions"],
                [{"name": "first", "passed": True}],
            )
            self.assertEqual(result["detail"]["available_details"], ["native_delta"])
            self.assertTrue(result["detail_truncated"])

    def test_project_inspection_resolves_legacy_managed_project_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")
            design_root = Path(temporary) / "managed"
            design_root.mkdir()
            project = SimpleNamespace(
                root=Path(temporary),
                design_root=design_root,
                state={"revision": 7},
            )
            design = SimpleNamespace(
                to_dict=Mock(return_value={"design_id": "board"}),
                content_hash=Mock(return_value="content-hash"),
            )
            managed = SimpleNamespace(
                design=design,
                assert_synchronized=Mock(),
            )
            view = {"project": {"id": "board"}, "state": {"revision": 7}}

            with (
                patch.object(service, "_open", return_value=project),
                patch.object(service, "_public_project", return_value=view),
                patch.object(
                    application,
                    "open_managed_project",
                    return_value=managed,
                ) as opener,
            ):
                result = service._inspect_pcb_tool("board", "inspect_design", {})

            opener.assert_called_once_with(design_root)
            managed.assert_synchronized.assert_called_once_with()
            self.assertIsNot(result, view)
            self.assertEqual(
                result["tool_result"],
                {
                    "design": {"design_id": "board"},
                    "content_hash": "content-hash",
                    "project_id": "board",
                    "revision": 7,
                },
            )

    def test_execute_pcb_tool_keeps_inspection_dispatch_on_inherited_method(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = ApplicationService(temporary, provider_name="auto")
            expected = {"tool_result": {"inspection": "design"}}
            with patch.object(
                service,
                "_inspect_pcb_tool",
                return_value=expected,
            ) as inspect:
                result = service.execute_pcb_tool(
                    "board",
                    "inspect_design",
                    {},
                    timeout=3.0,
                    expected_revision=4,
                )

            self.assertIs(result, expected)
            inspect.assert_called_once_with("board", "inspect_design", {})

    def test_library_inspection_keeps_resolver_patch_path(self):
        with patch(
            "pcbdraft.agent.part_resolver.LocalKiCadPartResolver"
        ) as resolver_type:
            resolver_type.return_value.find_ids.return_value = (
                "Device:R",
                "Device:R_US",
            )
            result = ApplicationService.inspect_installed_library(
                "search_symbols", {"query": "resistor"}
            )

        self.assertEqual(
            result,
            {
                "query": "resistor",
                "symbols": ["Device:R", "Device:R_US"],
            },
        )
        resolver_type.return_value.find_ids.assert_called_once_with(
            "resistor", limit=24
        )


if __name__ == "__main__":
    unittest.main()
