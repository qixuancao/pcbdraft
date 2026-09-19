from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.core.project import sha256_file
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.domain.semantic_rules import evaluate_semantic_rules
from pcbdraft.domain.task_contract import evaluate_task_coverage
from pcbdraft.services.application import ApplicationService
from pcbdraft.verification.validation import _constraint_registry_check
from tests.services.test_flat_pcb_tools import (
    _managed,
    _materialize_project,
    _passing_consistency,
    _seed_managed_project,
    _v2_design,
)


class AuditContractGateRegressionTests(unittest.TestCase):
    @staticmethod
    def _design_with_acceptance(*acceptance: str) -> Design:
        value = _v2_design().to_dict()
        value["requirements"] = [
            {
                "id": "req_user",
                "text": "Expose the requested user function.",
                "acceptance": list(acceptance),
                "risk": "low",
                "provenance": [],
            }
        ]
        return Design.from_dict(value)

    @staticmethod
    def _validation(design: Design, *, outcome: str = "pass") -> dict[str, object]:
        return {
            "run_id": "validation-1",
            "candidate_ready": outcome == "pass",
            "source_design_revision": 4,
            "source_content_hash": design.content_hash(),
            "levels": [
                {
                    "checks": [
                        {
                            "id": "l2.erc",
                            "state": "completed",
                            "outcome": outcome,
                            "evidence": ["erc.evidence.json"],
                        }
                    ]
                }
            ],
        }

    def test_unknown_blocking_constraint_is_rejected_by_flat_tool_schema(self) -> None:
        with self.assertRaisesRegex(ValidationError, "strict schema"):
            DEFAULT_PCB_TOOL_REGISTRY.normalize_arguments(
                "add_constraint",
                {
                    "value": {
                        "id": "unknown_rule",
                        "kind": "unsupported_release_rule",
                        "targets": ["board"],
                        "params": [],
                        "severity": "release_blocking",
                        "rationale": "Must not be silently accepted.",
                    }
                },
            )

    def test_retained_unknown_blocking_constraint_is_an_explicit_finding(self) -> None:
        value = _v2_design().to_dict()
        value["constraints"].append(
            {
                "id": "unknown_rule",
                "kind": "unsupported_release_rule",
                "targets": ["board"],
                "params": {},
                "severity": "required",
                "rationale": "Existing unsupported contract.",
                "provenance": [],
            }
        )
        design = Design.from_dict(value)

        findings = evaluate_semantic_rules(design, PartGraph.bundled())

        self.assertIn("intent.unsupported_constraint", {item.code for item in findings})
        self.assertTrue(any(item.object_id == "unknown_rule" for item in findings))

        constraint = next(
            item for item in design.constraints if item.id == "unknown_rule"
        )
        l3 = _constraint_registry_check(
            constraint,
            [item for item in findings if item.object_id == constraint.id],
            "design.json",
        )
        self.assertEqual(l3.id, "l3.constraint.unknown_rule")
        self.assertEqual((l3.state, l3.outcome), ("unavailable", "unknown"))
        self.assertTrue(l3.blocks_candidate)
        self.assertTrue(l3.blocks_production)
        self.assertEqual(l3.metrics["verification_support"], "unsupported")

    def test_flat_agent_can_manage_requirements_and_run_candidate_validation(
        self,
    ) -> None:
        self.assertEqual(
            DEFAULT_PCB_TOOL_REGISTRY.resolve("add_requirement").external_name,
            "pcb_add_requirement",
        )
        self.assertEqual(
            DEFAULT_PCB_TOOL_REGISTRY.resolve("validate_candidate").external_name,
            "pcb_validate_candidate",
        )

        service = object.__new__(ApplicationService)
        with patch.object(
            service,
            "validate_project",
            return_value={"candidate_ready": True},
        ) as aggregate:
            result = service.execute_pcb_tool(
                "board",
                "validate_candidate",
                {},
                timeout=12.0,
                expected_revision=4,
            )
        self.assertEqual(result, {"candidate_ready": True})
        aggregate.assert_called_once_with("board", timeout=12.0, expected_revision=4)

    def test_empty_or_unverified_requirement_contract_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, _design = _seed_managed_project(Path(temporary))
            with patch(
                "pcbdraft.services.application.open_managed_project",
                side_effect=lambda path: _managed(Path(path)),
            ):
                product = service.open_project(project_id)["product_status"]

        self.assertNotEqual(product["task_coverage"]["outcome"], "passed")
        self.assertFalse(product["task_coverage"]["complete"])

    def test_requirement_coverage_uses_current_checks_not_prose_or_manual_claims(
        self,
    ) -> None:
        verified = self._design_with_acceptance("check:l2.erc")
        passed = evaluate_task_coverage(
            verified,
            self._validation(verified),
            design_revision=4,
        )
        self.assertEqual(passed["outcome"], "passed")
        self.assertEqual(passed["items"][0]["evidence"], ["erc.evidence.json"])

        for acceptance in (
            "manual:engineer_says_pass",
            "check:model_says_pass",
            "the model says this requirement passed",
        ):
            with self.subTest(acceptance=acceptance):
                claimed = self._design_with_acceptance(acceptance)
                result = evaluate_task_coverage(
                    claimed,
                    self._validation(claimed),
                    design_revision=4,
                )
                self.assertEqual(result["outcome"], "blocked")
                self.assertFalse(result["complete"])

    def test_requirement_change_makes_old_check_evidence_stale(self) -> None:
        original = self._design_with_acceptance("check:l2.erc")
        validation = self._validation(original)
        value = original.to_dict()
        value["requirements"][0]["text"] = "Changed user requirement."
        changed = Design.from_dict(value)

        coverage = evaluate_task_coverage(changed, validation, design_revision=5)

        self.assertEqual(coverage["outcome"], "incomplete")
        self.assertEqual(coverage["items"][0]["state"], "stale")

    def test_manufacturing_export_is_rejected_before_current_candidate_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, _design = _seed_managed_project(Path(temporary))
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.export_manufacturing_output",
                    side_effect=AssertionError("export executed before gate"),
                ) as exporter,
                self.assertRaisesRegex(ValidationError, "candidate validation"),
            ):
                revision = service.open_project(project_id)["state"]["revision"]
                service.export_pcb_output(
                    project_id,
                    "export_bom",
                    timeout=12.0,
                    expected_revision=revision,
                )
            exporter.assert_not_called()

    def test_self_declared_candidate_gate_without_retained_report_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design = _seed_managed_project(Path(temporary))
            project = service._open(project_id)
            project.state["last_validation"] = {
                "run_id": "model-claim",
                "candidate_ready": True,
                "source_design_revision": project.state["design_revision"],
                "source_content_hash": design.content_hash(),
                "levels": [],
            }
            service._write_records(project.root, project.state, project.conversation)
            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.export_manufacturing_output",
                    side_effect=AssertionError("self-declared gate reached exporter"),
                ) as exporter,
                self.assertRaisesRegex(ValidationError, "retained report binding"),
            ):
                service.export_pcb_output(
                    project_id,
                    "export_bom",
                    timeout=12.0,
                    expected_revision=project.state["revision"],
                )
            exporter.assert_not_called()

    def test_manufacturing_export_succeeds_after_current_candidate_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, project_id, design = _seed_managed_project(Path(temporary))
            project = service._open(project_id)
            validation_root = project.root / "validation" / "validation-1"
            validation_root.mkdir(parents=True)
            validation_report = validation_root / "validation.json"
            atomic_write_json(
                validation_report,
                {
                    "schema": "pcbdraft-validation",
                    "version": 2,
                    "readiness": {"engineering_candidate": True},
                    "design": {"content_hash": design.content_hash()},
                },
            )
            atomic_write_json(
                validation_root / "receipt.json",
                {
                    "schema": "pcbdraft-validation-receipt",
                    "status": "complete",
                    "candidate_ready": True,
                    "design_content_hash": design.content_hash(),
                    "source_design_revision": project.state["design_revision"],
                },
            )
            project.state["status"] = "validated"
            project.state["last_validation"] = {
                "run_id": "validation-1",
                "report": validation_report.relative_to(project.root).as_posix(),
                "report_sha256": sha256_file(validation_report),
                "candidate_ready": True,
                "source_design_revision": project.state["design_revision"],
                "source_content_hash": design.content_hash(),
            }
            service._write_records(project.root, project.state, project.conversation)

            def export_output(managed, output, kind, *, timeout):
                del timeout
                output.mkdir(parents=True)
                receipt = output / "receipt.json"
                atomic_write_json(receipt, {"kind": kind})
                return SimpleNamespace(
                    root=output,
                    receipt_path=receipt,
                    design_content_hash=managed.design.content_hash(),
                    artifacts=("bom.csv",),
                )

            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.export_manufacturing_output",
                    side_effect=export_output,
                ) as exporter,
            ):
                revision = service.open_project(project_id)["state"]["revision"]
                result = service.export_pcb_output(
                    project_id,
                    "export_bom",
                    timeout=12.0,
                    expected_revision=revision,
                )

        exporter.assert_called_once()
        self.assertEqual(result["tool_result"]["export"], "export_bom")
        self.assertFalse(result["tool_result"]["production_ready"])

    def test_requirement_operation_invalidates_candidate_gate_and_old_export(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design = self._design_with_acceptance("check:l2.erc")
            service, project_id, _ = _seed_managed_project(
                Path(temporary), design=design
            )
            project = service._open(project_id)
            validation_root = project.root / "validation" / "validation-1"
            validation_root.mkdir(parents=True)
            validation_report = validation_root / "validation.json"
            atomic_write_json(
                validation_report,
                {
                    "schema": "pcbdraft-validation",
                    "version": 2,
                    "readiness": {"engineering_candidate": True},
                    "design": {"content_hash": design.content_hash()},
                },
            )
            atomic_write_json(
                validation_root / "receipt.json",
                {
                    "schema": "pcbdraft-validation-receipt",
                    "status": "complete",
                    "candidate_ready": True,
                    "design_content_hash": design.content_hash(),
                    "source_design_revision": project.state["design_revision"],
                },
            )
            project.state["status"] = "validated"
            project.state["last_validation"] = {
                "run_id": "validation-1",
                "report": validation_report.relative_to(project.root).as_posix(),
                "report_sha256": sha256_file(validation_report),
                "candidate_ready": True,
                "source_design_revision": project.state["design_revision"],
                "source_content_hash": design.content_hash(),
            }
            service._write_records(project.root, project.state, project.conversation)

            with (
                patch(
                    "pcbdraft.services.application.open_managed_project",
                    side_effect=lambda path: _managed(Path(path)),
                ),
                patch(
                    "pcbdraft.services.application.load_generation_request",
                    return_value=object(),
                ),
                patch(
                    "pcbdraft.services.application.materialize_managed_design",
                    side_effect=_materialize_project,
                ),
                patch(
                    "pcbdraft.services.application.inspect_native_consistency",
                    return_value=_passing_consistency(),
                ),
            ):
                revision = service.open_project(project_id)["state"]["revision"]
                changed = service.apply_pcb_operation(
                    project_id,
                    "update_requirement",
                    {
                        "value": {
                            "id": "req_user",
                            "text": "Changed user requirement.",
                            "acceptance": ["check:l2.drc_connectivity"],
                            "risk": "low",
                        }
                    },
                    timeout=12.0,
                    expected_revision=revision,
                )
                self.assertIsNone(changed["state"]["last_validation"])
                with self.assertRaisesRegex(ValidationError, "candidate validation"):
                    service.export_pcb_output(
                        project_id,
                        "export_bom",
                        timeout=12.0,
                        expected_revision=changed["state"]["revision"],
                    )

    def test_design_requirement_change_invalidates_prior_validation(self) -> None:
        design = _v2_design()
        value = design.to_dict()
        value["requirements"] = [
            {
                "id": "req_led",
                "text": "Expose an LED output.",
                "acceptance": ["check:l2.erc"],
                "risk": "low",
                "provenance": [],
            }
        ]
        changed = copy.deepcopy(value)
        changed["requirements"][0]["acceptance"] = ["check:l2.drc_connectivity"]
        self.assertNotEqual(
            Design.from_dict(value).content_hash(),
            Design.from_dict(changed).content_hash(),
        )


if __name__ == "__main__":
    unittest.main()
