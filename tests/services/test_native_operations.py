from __future__ import annotations

import unittest
from unittest.mock import patch

from pcbdraft.services import application, native_operations


class NativeOperationModuleTests(unittest.TestCase):
    def test_application_reexports_extracted_operation_contracts(self) -> None:
        self.assertIs(
            application._PCBOperationPostconditionError,
            native_operations._PCBOperationPostconditionError,
        )
        self.assertIs(
            application._native_postconditions,
            native_operations._native_postconditions,
        )
        self.assertIs(
            application._native_delta_postconditions,
            native_operations._native_delta_postconditions,
        )
        self.assertIs(
            application._fatal_drc_count,
            native_operations._fatal_drc_count,
        )

    def test_application_board_projection_keeps_inspector_patch_point(self) -> None:
        managed = object()
        projection = object()
        with (
            patch.object(application, "inspect_native_board") as inspector,
            patch.object(
                application,
                "_native_board_projection_impl",
                return_value=projection,
            ) as implementation,
        ):
            self.assertIs(application._native_board_projection(managed), projection)
        implementation.assert_called_once_with(
            managed,
            inspector=inspector,
            is_complete=application._native_board_projection_complete,
        )

    def test_application_schematic_projection_keeps_inspector_patch_point(
        self,
    ) -> None:
        managed = object()
        projection = object()
        with (
            patch.object(application, "inspect_native_schematic") as inspector,
            patch.object(
                application,
                "_native_schematic_projection_impl",
                return_value=projection,
            ) as implementation,
        ):
            self.assertIs(application._native_schematic_projection(managed), projection)
        implementation.assert_called_once_with(
            managed,
            inspector=inspector,
            is_complete=application._native_schematic_projection_complete,
        )

    def test_fatal_drc_count_uses_nested_full_report(self) -> None:
        document = {
            "display": [{"severity": "warning", "type": "shorting_items"}],
            "groups": [
                {
                    "items": [
                        {"severity": "error", "type": "clearance_violation"},
                        {"severity": "error", "type": "silk_overlap"},
                        {"severity": "ERROR", "type": "board_edge_clearance"},
                    ]
                }
            ],
        }
        self.assertEqual(native_operations._fatal_drc_count(document), 2)


if __name__ == "__main__":
    unittest.main()
