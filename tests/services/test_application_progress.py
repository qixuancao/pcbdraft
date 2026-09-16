"""Direct and compatibility coverage for application progress projections."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.consistency import (
    NativeBoardProjection,
    NativeConsistencyReport,
)
from pcbdraft.kicad.routing import RoutingFailure
from pcbdraft.services import application, application_progress
from pcbdraft.services.progress import (
    EngineeringStage,
    EvidenceCheck,
    MetricValue,
    ProgressVector,
    StageProjection,
)


class ApplicationProgressTests(unittest.TestCase):
    def test_application_keeps_original_helper_patch_points(self) -> None:
        helper_names = (
            "_progress_vector",
            "_progress_stage_evidence",
            "_attach_progress",
            "_transaction_progress_projection",
            "_route_state_key",
            "_route_state_record",
            "_route_state_key_from_record",
            "_routing_failure_retry_key",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(application, name), getattr(application_progress, name)
                )

        tree = ast.parse(
            Path(application_progress.__file__).read_text(encoding="utf-8")
        )
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertNotIn("pcbdraft.services.application", imports)

    def test_progress_and_stage_evidence_are_revision_bound(self) -> None:
        design = cast(
            Design,
            SimpleNamespace(
                components=(
                    SimpleNamespace(part_id="part-a", placement=None, attributes={}),
                    SimpleNamespace(
                        part_id="part-b",
                        placement=None,
                        attributes={"exclude_from_board": True},
                    ),
                ),
                native_intent=SimpleNamespace(routes=("route",), vias=()),
                issues=list,
            ),
        )
        graph = cast(
            PartGraph,
            SimpleNamespace(get=lambda _part_id: SimpleNamespace(footprint="FP")),
        )
        consistency = NativeConsistencyReport(
            4, "evaluated", "evaluated", "not_evaluated", ()
        )
        board = NativeBoardProjection("evaluated", (), (), (), 2, "not_evaluated")

        progress = application_progress._progress_vector(
            design,
            graph,
            4,
            consistency=consistency,
            board=board,
            fatal_drc=MetricValue.known(0, 4),
            routing_failure_count=1,
        )
        self.assertEqual(progress.semantic_native_mismatch_count.value, 0)
        self.assertEqual(progress.unresolved_connection_count.value, 2)
        self.assertEqual(progress.unplaced_component_count.value, 1)
        self.assertEqual(progress.routing_failure_count.value, 1)

        evidence = application_progress._progress_stage_evidence(
            design,
            4,
            requirements_frozen=True,
            consistency=consistency,
            progress=progress,
            erc_check=EvidenceCheck.unknown(4),
            drc_check=EvidenceCheck.unknown(4),
        )
        self.assertTrue(evidence.requirements_frozen.passed)
        self.assertTrue(evidence.routing_started.passed)
        self.assertFalse(evidence.native_connectivity_confirmed.passed)

    def test_receipt_projection_is_bounded_and_deep_copied(self) -> None:
        receipt = {
            "version": 1,
            "status": "committed",
            "progress_before": {"metrics": [{"value": 2}]},
            "application_progress": {"classification": "improved"},
            "unrelated_private_detail": {"secret": True},
        }
        projected = application_progress._transaction_progress_projection(receipt)
        receipt["progress_before"]["metrics"][0]["value"] = 99

        self.assertEqual(projected["progress_before"]["metrics"][0]["value"], 2)
        self.assertNotIn("unrelated_private_detail", projected)

    def test_attach_progress_and_route_retry_validation(self) -> None:
        unknown = ProgressVector.unknown(3)
        known = unknown.replace_metric(
            "unresolved_connection_count", MetricValue.known(0, 3)
        )
        stage = StageProjection(
            EngineeringStage.NOT_STARTED, False, ("requirements_frozen",)
        )
        receipt: dict[str, Any] = {}
        application_progress._attach_progress(receipt, unknown, known, stage, stage)
        self.assertEqual(receipt["progress_delta"]["classification"], "indeterminate")

        failure = RoutingFailure(
            "no_legal_channel",
            "power",
            ("U1.1", "J1.1"),
            expanded_nodes=12,
            blocking_summary="blocked",
            recommendations=("move_component",),
            state_revision=3,
            state_context=("placement=U1@1,2/0/front",),
        )
        retained = failure.to_dict()
        self.assertEqual(
            application_progress._routing_failure_retry_key(retained),
            failure.retry_key,
        )
        retained["retry_key"] = "tampered"
        with self.assertRaisesRegex(ValidationError, "retry key is inconsistent"):
            application_progress._routing_failure_retry_key(retained)

        state = {
            "design_revision": 3,
            "net_id": "power",
            "context": ["layers=board:2", "order=0/1"],
        }
        self.assertEqual(
            application_progress._route_state_key_from_record(state),
            "revision=3|layers=board:2|order=0/1",
        )


if __name__ == "__main__":
    unittest.main()
