from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pcbdraft.core.errors import ValidationError
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.project_libraries import (
    materialize_project_libraries,
    merge_project_library_tables,
)
from pcbdraft.services.managed import _validate_manifest


def _custom_graph() -> PartGraph:
    part = PartGraph.installed_kicad_part(
        {
            "id": "custom.bcon",
            "kind": "connector",
            "description": "test custom connector",
            "symbol": "LQEDA:BCON",
            "footprint": "LQEDA:SOP_BCON",
            "bom": True,
            "pins": [
                {
                    "number": "1",
                    "name": "A",
                    "electrical_type": "passive",
                    "functions": [],
                    "required": True,
                    "footprint_pad": "1",
                }
            ],
        },
        footprint_sha256="a" * 64,
    )
    return PartGraph((part,), license_id="test", source="test")


def _design() -> SimpleNamespace:
    return SimpleNamespace(
        components=(SimpleNamespace(part_id="custom.bcon"),),
    )


class ProjectLibraryTests(unittest.TestCase):
    def test_custom_resources_are_minimal_and_project_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_symbols = root / "source-symbols"
            source_footprints = root / "source-footprints"
            stock_symbols = root / "stock-symbols"
            stock_footprints = root / "stock-footprints"
            for path in (
                source_symbols,
                source_footprints / "LQEDA.pretty",
                stock_symbols,
                stock_footprints,
            ):
                path.mkdir(parents=True)
            (source_symbols / "LQEDA.kicad_sym").write_text(
                '(kicad_symbol_lib (symbol "BCON"))\n', encoding="utf-8"
            )
            (source_footprints / "LQEDA.pretty" / "SOP_BCON.kicad_mod").write_text(
                '(footprint "SOP_BCON")\n', encoding="utf-8"
            )
            project = root / "project"
            project.mkdir()
            files = materialize_project_libraries(
                project,
                _design(),
                _custom_graph(),
                symbol_root=source_symbols,
                footprint_root=source_footprints,
                stock_symbol_root=stock_symbols,
                stock_footprint_root=stock_footprints,
            )
            self.assertEqual(
                set(files),
                {
                    "library:symbol:LQEDA",
                    "library:footprint:LQEDA:SOP_BCON",
                    "symbol_table",
                    "footprint_table",
                },
            )
            self.assertIn(
                '(uri "${KIPRJMOD}/libraries/symbols/LQEDA.kicad_sym")',
                (project / "sym-lib-table").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '(uri "${KIPRJMOD}/libraries/footprints/LQEDA.pretty")',
                (project / "fp-lib-table").read_text(encoding="utf-8"),
            )
            self.assertTrue((project / files["library:symbol:LQEDA"]).is_file())
            self.assertTrue(
                (project / files["library:footprint:LQEDA:SOP_BCON"]).is_file()
            )

    def test_existing_entries_are_preserved_and_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            table = root / "sym-lib-table"
            table.write_text(
                "(sym_lib_table\n\t(version 7)\n"
                '\t(lib (name "Keep") (type "KiCad") '
                '(uri "${KIPRJMOD}/keep.kicad_sym") (options "") (descr "keep"))\n)\n',
                encoding="utf-8",
            )
            merge_project_library_tables(
                root,
                symbol_entries=(
                    ("LQEDA", "${KIPRJMOD}/libraries/symbols/LQEDA.kicad_sym"),
                ),
            )
            text = table.read_text(encoding="utf-8")
            self.assertIn('(name "Keep")', text)
            self.assertIn('(name "LQEDA")', text)
            with self.assertRaisesRegex(ValidationError, "conflicts"):
                merge_project_library_tables(
                    root,
                    symbol_entries=(("LQEDA", "${KIPRJMOD}/other.kicad_sym"),),
                )

    def test_stock_matching_resources_are_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_symbols = root / "symbols"
            source_footprints = root / "footprints"
            stock_symbols = root / "stock-symbols"
            stock_footprints = root / "stock-footprints"
            (source_symbols).mkdir()
            (source_footprints / "LQEDA.pretty").mkdir(parents=True)
            (stock_symbols).mkdir()
            (stock_footprints / "LQEDA.pretty").mkdir(parents=True)
            symbol = '(kicad_symbol_lib (symbol "BCON"))\n'
            footprint = '(footprint "SOP_BCON")\n'
            (source_symbols / "LQEDA.kicad_sym").write_text(symbol, encoding="utf-8")
            (stock_symbols / "LQEDA.kicad_sym").write_text(symbol, encoding="utf-8")
            (source_footprints / "LQEDA.pretty" / "SOP_BCON.kicad_mod").write_text(
                footprint, encoding="utf-8"
            )
            (stock_footprints / "LQEDA.pretty" / "SOP_BCON.kicad_mod").write_text(
                footprint, encoding="utf-8"
            )
            project = root / "project"
            project.mkdir()
            files = materialize_project_libraries(
                project,
                _design(),
                _custom_graph(),
                symbol_root=source_symbols,
                footprint_root=source_footprints,
                stock_symbol_root=stock_symbols,
                stock_footprint_root=stock_footprints,
            )
            self.assertEqual(files, {})
            self.assertFalse((project / "sym-lib-table").exists())
            self.assertFalse((project / "fp-lib-table").exists())

    def test_manifest_binds_nested_library_resources_to_exact_paths(self) -> None:
        files = {
            "manifest": "project.pcbdraft.json",
            "requirements": "requirements.pcbreq.json",
            "ir": "design.pcbir.json",
            "schematic": "board.kicad_sch",
            "board": "board.kicad_pcb",
            "kicad_project": "board.kicad_pro",
            "worker_receipt": "board.worker-result.json",
            "symbol_table": "sym-lib-table",
            "library:symbol:LQEDA": "libraries/symbols/LQEDA.kicad_sym",
            "footprint_table": "fp-lib-table",
            "library:footprint:LQEDA:SOP_BCON": (
                "libraries/footprints/LQEDA.pretty/SOP_BCON.kicad_mod"
            ),
        }
        manifest = {
            "schema": "pcbdraft-managed-project",
            "version": 1,
            "runtime_version": "test",
            "design": {
                "id": "design",
                "name": "Design",
                "revision": "A",
                "content_hash": "a" * 64,
            },
            "files": files,
            "hashes": {key: "b" * 64 for key in files if key != "manifest"},
            "generation": {},
            "native_snapshots": {
                "schematic": {},
                "board": {},
                "project": {},
            },
            "sync": {},
        }
        _validate_manifest(manifest)
        manifest["files"]["library:symbol:LQEDA"] = "libraries/other/LQEDA.kicad_sym"
        with self.assertRaisesRegex(ValidationError, "file map"):
            _validate_manifest(manifest)
        manifest["files"]["library:symbol:LQEDA"] = "libraries/symbols/LQEDA.kicad_sym"
        manifest["files"]["requirements"] = "libraries/requirements.pcbreq.json"
        with self.assertRaisesRegex(ValidationError, "file map"):
            _validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
