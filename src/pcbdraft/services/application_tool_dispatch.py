"""Registry-bound PCB tool dispatch at the application boundary.

The host ApplicationService remains the composition root and owns every
inspection, output, registration, and mutation implementation. This mixin only
routes an already validated registry tool name and preserves the caller's
revision and timeout arguments.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ApplicationPCBToolDispatchMixin:
    """Route one closed-registry tool call to its owning application workflow."""

    @staticmethod
    def _tool_dispatch_validation_error(message: str) -> Exception:
        raise NotImplementedError

    def execute_pcb_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Execute one registry-bound flat PCB operation.

        The model-facing name is already resolved by the closed registry. This
        method owns the final service dispatch and always returns one public
        project view augmented with a bounded, fact-only operation result.
        """

        if tool_name == "inspect_project":
            return self._with_tool_result(
                self.open_project(project_id), {"inspection": "project"}
            )
        if tool_name in {
            "inspect_design",
            "inspect_component",
            "inspect_net",
            "inspect_board",
            "inspect_events",
            "inspect_evidence",
            "inspect_transaction",
        }:
            return self._inspect_pcb_tool(project_id, tool_name, arguments)
        if tool_name == "observe_board_region":
            view = self.open_project(project_id)
            state = view.get("state")
            if (
                not isinstance(state, Mapping)
                or state.get("revision") != expected_revision
            ):
                raise self._tool_dispatch_validation_error(
                    "project changed before the board region could be observed"
                )
            return self._with_tool_result(
                view,
                {"operation": "observe_board_region", **arguments},
            )
        if tool_name in {
            "search_symbols",
            "describe_symbol",
            "search_footprints",
            "describe_footprint",
        }:
            return self._inspect_library_tool(project_id, tool_name, arguments)
        if tool_name in {"search_parts", "describe_part"}:
            return self._inspect_part_tool(project_id, tool_name, arguments)
        if tool_name == "register_kicad_part":
            return self.register_kicad_part(
                project_id,
                arguments["value"],
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {
            "check_semantics",
            "check_connectivity",
            "run_erc",
            "run_drc",
        }:
            return self.run_pcb_check(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name == "validate_candidate":
            return self.validate_project(
                project_id,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        if tool_name in {"render_schematic", "render_board", "render_3d"}:
            return self.render_pcb_output(
                project_id,
                tool_name,
                timeout=min(timeout, 600.0),
                expected_revision=expected_revision,
            )
        if tool_name in {
            "export_gerbers",
            "export_drill",
            "export_bom",
            "export_pick_place",
            "export_step",
        }:
            return self.export_pcb_output(
                project_id,
                tool_name,
                timeout=timeout,
                expected_revision=expected_revision,
            )
        return self.apply_pcb_operation(
            project_id,
            tool_name,
            arguments,
            timeout=timeout,
            expected_revision=expected_revision,
        )
