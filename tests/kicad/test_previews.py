from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pcbdraft.core.process import CommandResult
from pcbdraft.kicad.previews import generate_preview


class PreviewSelectionTests(unittest.TestCase):
    def test_render_board_includes_real_png_for_model_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            design = SimpleNamespace(content_hash=lambda: "a" * 64)
            project = SimpleNamespace(
                root=root,
                schematic_path=root / "board.kicad_sch",
                board_path=root / "board.kicad_pcb",
                design=design,
                assert_synchronized=lambda: None,
            )
            calls: list[tuple[str, ...]] = []

            def run(argv, **kwargs):
                del kwargs
                calls.append(tuple(argv))
                output = Path(argv[argv.index("--output") + 1])
                if output.suffix == ".svg":
                    output.write_text("<svg/>", encoding="utf-8")
                elif output.suffix == ".png":
                    image = io.BytesIO()
                    Image.new("RGB", (2, 2), (10, 20, 30)).save(image, format="PNG")
                    output.write_bytes(image.getvalue())
                else:
                    self.fail(f"unexpected preview output: {output}")
                return CommandResult(tuple(argv), 0, b"", b"", 0.01)

            with (
                patch(
                    "pcbdraft.kicad.previews.find_kicad_cli",
                    return_value="/usr/bin/kicad-cli",
                ),
                patch("pcbdraft.kicad.previews.run_command", side_effect=run),
                patch(
                    "pcbdraft.kicad.previews.open_managed_project",
                    return_value=project,
                ),
            ):
                bundle = generate_preview(
                    root, root / "preview", "render_board", timeout=5
                )

            self.assertEqual(set(bundle.files), {"board_svg", "board_render"})
            self.assertEqual(
                [argv[1:3] for argv in calls],
                [("pcb", "export"), ("pcb", "render")],
            )
            with Image.open(bundle.files["board_render"]) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (2, 2))


if __name__ == "__main__":
    unittest.main()
