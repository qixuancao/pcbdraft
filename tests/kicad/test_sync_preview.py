from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.kicad.sync import preview_kicad_import


class SyncPreviewSnapshotTests(unittest.TestCase):
    def test_native_inputs_cannot_change_during_preview_parsing(self) -> None:
        for changed_name in (None, "board", "manifest", "schematic", "kicad_project"):
            with (
                self.subTest(changed=changed_name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                paths = {
                    name: root / name
                    for name in ("board", "manifest", "schematic", "kicad_project")
                }
                for path in paths.values():
                    path.write_text("original", encoding="utf-8")
                managed = SimpleNamespace(
                    root=root,
                    board_path=paths["board"],
                    manifest_path=paths["manifest"],
                    schematic_path=paths["schematic"],
                    project_path=paths["kicad_project"],
                    manifest={
                        "files": {name: name for name in paths},
                        "native_snapshots": {
                            "schematic": {},
                            "project": {},
                            "board": {},
                        },
                    },
                    design=SimpleNamespace(content_hash=lambda: "a" * 64),
                    graph=object(),
                    drift=lambda: (),
                )

                def inspect(*_args, changed_name=changed_name, paths=paths, **_kwargs):
                    if changed_name:
                        paths[changed_name].write_text(
                            "changed during parsing", encoding="utf-8"
                        )
                    return {}

                with (
                    patch(
                        "pcbdraft.kicad.sync.open_managed_project", return_value=managed
                    ),
                    patch(
                        "pcbdraft.kicad.sync.inspect_native_schematic", return_value={}
                    ),
                    patch(
                        "pcbdraft.kicad.sync.inspect_native_project", return_value={}
                    ),
                    patch(
                        "pcbdraft.kicad.sync.inspect_native_board", side_effect=inspect
                    ),
                ):
                    if changed_name:
                        with self.assertRaisesRegex(
                            ValidationError, "after synchronization preview"
                        ):
                            preview_kicad_import(root)
                    else:
                        preview = preview_kicad_import(root)
                        self.assertFalse(preview.has_changes)
                        self.assertRegex(preview.review_token, r"^[0-9a-f]{64}$")
