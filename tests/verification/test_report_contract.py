from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.process import CommandResult
from pcbdraft.verification.gates import run_gate
from pcbdraft.verification.rule_evidence import capture_rule_evidence
from pcbdraft.verification.validation import run_individual_check


class ReportContractTests(unittest.TestCase):
    def test_missing_or_malformed_evidence_never_passes_any_report_entrypoint(
        self,
    ) -> None:
        for kind in ("erc", "drc"):
            valid = {
                "$schema": f"https://schemas.kicad.org/{kind}.v1.json",
                "kicad_version": "10.0.5-test",
                "included_severities": ["error", "warning"],
                **(
                    {"sheets": [{"violations": []}]}
                    if kind == "erc"
                    else {
                        "violations": [],
                        "unconnected_items": [],
                        "schematic_parity": [],
                    }
                ),
            }
            header = {
                key: value
                for key, value in valid.items()
                if key.startswith("$") or key == "kicad_version"
            }
            invalid_severity = copy.deepcopy(valid)
            if kind == "erc":
                invalid_severity["sheets"][0]["violations"] = [{"severity": "unknown"}]
            else:
                invalid_severity["violations"] = [{"severity": "unknown"}]
            bad_section = {**valid, ("sheets" if kind == "erc" else "violations"): {}}
            truncated = {**valid, "report_truncated": True}
            incomplete_severities = {**valid, "included_severities": ["warning"]}
            for label, document, complete in (
                ("valid", valid, True),
                ("empty_object", {}, False),
                ("empty_list", [], False),
                ("headers_only", header, False),
                ("bad_section", bad_section, False),
                ("unknown_severity", invalid_severity, False),
                ("truncated", truncated, False),
                ("warnings_only", incomplete_severities, False),
            ):
                with (
                    self.subTest(kind=kind, document=label),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    project_root = root / "project"
                    project_root.mkdir()
                    board = project_root / "board.kicad_pcb"
                    schematic = project_root / "board.kicad_sch"
                    board.write_text("test source", encoding="utf-8")
                    schematic.write_text("test source", encoding="utf-8")
                    managed = SimpleNamespace(
                        root=project_root,
                        board_path=board,
                        schematic_path=schematic,
                        graph=object(),
                        design=SimpleNamespace(content_hash=lambda: "a" * 64),
                        assert_synchronized=lambda: None,
                    )

                    def command(argv, document=document, **_kwargs):
                        Path(argv[argv.index("--output") + 1]).write_text(
                            json.dumps(document), encoding="utf-8"
                        )
                        return CommandResult(tuple(argv), 0, b"", b"", 0.0)

                    with (
                        patch(
                            "pcbdraft.verification.validation.open_managed_project",
                            return_value=managed,
                        ),
                        patch(
                            "pcbdraft.verification.validation.find_kicad_cli",
                            return_value="fake-cli",
                        ),
                        patch(
                            "pcbdraft.verification.validation.run_command",
                            side_effect=command,
                        ),
                    ):
                        result = run_individual_check(
                            project_root,
                            f"run_{kind}",
                            output=root / "individual",
                            timeout=1,
                        )
                    self.assertEqual(result.outcome, "pass" if complete else "unknown")
                    self.assertEqual(
                        result.state, "completed" if complete else "unavailable"
                    )
                    with patch(
                        "pcbdraft.verification.gates.run_command", side_effect=command
                    ):
                        gate = run_gate(
                            name=kind,
                            input_file=board if kind == "drc" else schematic,
                            raw_output=root / "raw.json",
                            executable="fake-cli",
                            deadline=time.monotonic() + 1,
                            redactions={},
                        )
                    self.assertEqual(gate.tool_status == "ok", complete)
                    self.assertEqual(gate.error_count, 0 if complete else None)
                    evidence = capture_rule_evidence(
                        kind=kind,
                        raw_report=root / "raw.json",
                        output=root / "evidence.json",
                        source_file=board,
                        canonical_revision=1,
                        design_revision=1,
                        design_content_hash="a" * 64,
                    )
                    self.assertEqual(evidence.complete, complete)
                    self.assertEqual(evidence.error_count, 0 if complete else None)
