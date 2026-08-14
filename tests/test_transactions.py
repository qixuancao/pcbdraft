from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from pcbdraft.errors import PCBDraftError, ValidationError
from pcbdraft.io import atomic_write_bytes, atomic_write_json
from pcbdraft.ir import Design, load_design, save_design
from pcbdraft.locking import ResourceLock
from pcbdraft.operations import ChangeSet
from pcbdraft.transactions import (
    apply_transaction,
    prepare_transaction,
    recover_transaction,
    undo_transaction,
)
from tests.design_factory import minimal_design_dict


def metadata_change(design: Design, change_id: str = "metadata_change") -> ChangeSet:
    return ChangeSet.from_dict(
        {
            "schema": "pcbdraft-change-set",
            "version": 1,
            "id": change_id,
            "base_hash": design.content_hash(),
            "intent": "Record an audited metadata field.",
            "actor": "unit-test",
            "operations": [
                {
                    "id": "metadata_op",
                    "op": "set_metadata",
                    "args": {"key": "audited", "value": True},
                    "expected": {"fixture": True},
                    "reason": "Exercise semantic journaling.",
                }
            ],
            "provenance": ["tests/test_transactions.py"],
        }
    )


class SemanticTransactionTests(unittest.TestCase):
    def make_paths(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        runs = root / "runs"
        project.mkdir()
        design_path = project / "design.pcbir.json"
        save_design(design_path, Design.from_dict(minimal_design_dict()))
        return design_path, runs

    def test_preview_apply_idempotent_apply_and_undo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design_path, runs = self.make_paths(Path(temporary))
            before = design_path.read_bytes()
            design = load_design(design_path)
            run_dir = prepare_transaction(
                design_path, metadata_change(design), output_parent=runs
            )
            self.assertEqual(design_path.read_bytes(), before)
            receipt = json.loads((run_dir / "receipt.json").read_text())
            self.assertEqual(receipt["status"], "ready")
            self.assertTrue((run_dir / "semantic_diff.json").is_file())

            apply_transaction(run_dir)
            self.assertTrue(load_design(design_path).metadata["audited"])
            applied_bytes = design_path.read_bytes()
            apply_transaction(run_dir)
            self.assertEqual(design_path.read_bytes(), applied_bytes)

            undo_transaction(run_dir)
            self.assertEqual(design_path.read_bytes(), before)
            undo_transaction(run_dir)
            self.assertEqual(design_path.read_bytes(), before)
            receipt = json.loads((run_dir / "receipt.json").read_text())
            self.assertEqual(receipt["status"], "undone")

    def test_source_or_staging_drift_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design_path, runs = self.make_paths(Path(temporary))
            design = load_design(design_path)
            run_dir = prepare_transaction(
                design_path, metadata_change(design), output_parent=runs
            )
            drifted = design_path.read_bytes() + b" "
            atomic_write_bytes(design_path, drifted)
            with self.assertRaisesRegex(ValidationError, "drifted"):
                apply_transaction(run_dir)
            self.assertEqual(design_path.read_bytes(), drifted)

        with tempfile.TemporaryDirectory() as temporary:
            design_path, runs = self.make_paths(Path(temporary))
            design = load_design(design_path)
            run_dir = prepare_transaction(
                design_path, metadata_change(design), output_parent=runs
            )
            atomic_write_bytes(run_dir / "after.pcbir.json", b"{}\n")
            with self.assertRaisesRegex(ValidationError, "hash mismatch"):
                apply_transaction(run_dir)
            self.assertEqual(load_design(design_path), design)

    def test_recovery_restores_only_known_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design_path, runs = self.make_paths(Path(temporary))
            before = design_path.read_bytes()
            design = load_design(design_path)
            run_dir = prepare_transaction(
                design_path, metadata_change(design), output_parent=runs
            )
            receipt_path = run_dir / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            staged = (run_dir / "after.pcbir.json").read_bytes()
            atomic_write_bytes(run_dir / "backup.pcbir.json", before)
            receipt["status"] = "applying"
            receipt["backup"] = "backup.pcbir.json"
            atomic_write_json(receipt_path, receipt)
            atomic_write_bytes(design_path, staged)
            recover_transaction(run_dir)
            self.assertEqual(design_path.read_bytes(), before)
            self.assertEqual(
                json.loads(receipt_path.read_text())["status"], "recovered"
            )

    def test_symlink_hardlink_and_lock_contention_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            design_path, runs = self.make_paths(root)
            link = root / "link.pcbir.json"
            link.symlink_to(design_path)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                prepare_transaction(
                    link, metadata_change(load_design(design_path)), output_parent=runs
                )

            hardlink = root / "hard.pcbir.json"
            os.link(design_path, hardlink)
            with self.assertRaisesRegex(ValidationError, "single-link"):
                prepare_transaction(
                    design_path,
                    metadata_change(load_design(design_path)),
                    output_parent=runs,
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resource = root / "resource"
            resource.write_text("x")
            locks = root / "locks"
            with (
                ResourceLock(resource, locks),
                self.assertRaisesRegex(PCBDraftError, "locked"),
            ):
                ResourceLock(resource, locks, timeout=0.02).acquire()


if __name__ == "__main__":
    unittest.main()
