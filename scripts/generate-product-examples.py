#!/usr/bin/env python3
"""Generate or verify the three accepted CopperWright product examples."""

from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from copperwright.io import (
    atomic_write_json,
    atomic_write_text,
    load_json_limited,
    make_directory,
)
from copperwright.managed import generate_managed_project, open_managed_project
from copperwright.previews import generate_previews
from copperwright.profiles import build_requirements, get_product_profile
from copperwright.project import sha256_file
from copperwright.requirements import compile_requirements
from copperwright.validation import validate_managed_project

REPO = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Example:
    directory: str
    profile_id: str
    name: str
    design_id: str
    layers: int


EXAMPLES = (
    Example(
        "i2c_temperature_controller",
        "low_voltage_i2c_controller_v1",
        "CopperWright I2C temperature controller",
        "copperwright_i2c_temperature_controller",
        2,
    ),
    Example(
        "spi_environment_sensor",
        "low_voltage_spi_environment_v1",
        "CopperWright SPI environment sensor",
        "copperwright_spi_environment_sensor",
        2,
    ),
    Example(
        "uart_ldo_controller",
        "low_voltage_uart_ldo_controller_v1",
        "CopperWright UART LDO controller",
        "copperwright_uart_ldo_controller",
        4,
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO / "examples" / "product_profiles",
    )
    return parser.parse_args()


def requirements(example: Example):
    return build_requirements(
        example.profile_id,
        design_name=example.name,
        design_id=example.design_id,
        layers=example.layers,
        width_mm=45,
        height_mm=30,
        source_locator=(
            f"examples/product_profiles/{example.directory}/requirements.json"
        ),
        source_date="2026-08-13",
    )


def normalize_preview_svgs(root: Path) -> None:
    """Remove KiCad export line-end spaces and keep its receipt exact."""

    receipt_path = root / "receipt.json"
    receipt = load_json_limited(receipt_path, 4 * 1024 * 1024)
    inventory = receipt["files"]
    by_path = {item["path"]: item for item in inventory.values()}
    changed = False
    for path in sorted(root.rglob("*.svg")):
        relative = path.relative_to(root).as_posix()
        normalized = (
            "\n".join(
                line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()
            )
            + "\n"
        )
        if path.read_text(encoding="utf-8") != normalized:
            atomic_write_text(path, normalized)
            changed = True
        item = by_path[relative]
        item["bytes"] = path.stat().st_size
        item["sha256"] = sha256_file(path, max_bytes=64 * 1024 * 1024)
    if changed:
        atomic_write_json(receipt_path, receipt)


def verify_existing(target: Path, example: Example) -> dict[str, object]:
    normalize_preview_svgs(target / "previews")
    expected = requirements(example)
    actual_requirements = load_json_limited(
        target / "requirements.json", 4 * 1024 * 1024
    )
    if actual_requirements != expected.to_dict():
        raise RuntimeError(f"example requirements drifted: {target}")
    project = open_managed_project(target / "project")
    project.assert_synchronized()
    expected_hash = compile_requirements(expected).content_hash()
    if project.design.content_hash() != expected_hash:
        raise RuntimeError(f"example semantic design drifted: {target}")
    acceptance = load_json_limited(target / "acceptance.json", 4 * 1024 * 1024)
    if (
        acceptance.get("design_content_hash") != expected_hash
        or acceptance.get("candidate_ready") is not True
        or acceptance.get("production_ready") is not False
    ):
        raise RuntimeError(f"example acceptance summary drifted: {target}")
    return acceptance


def readme(example: Example, component_count: int, constraint_count: int) -> str:
    profile = get_product_profile(example.profile_id)
    return f"""# {profile.title}

This is a generated, directly openable KiCad 10 example for the verified
`{example.profile_id}` CopperWright profile.

- Board: 45 mm × 30 mm, {example.layers} copper layers
- Semantic components: {component_count}
- Deterministic constraints: {constraint_count}
- Candidate gate: passed locally through real KiCad ERC/DRC and CopperWright L0–L3
- Production claim: **false**; live sourcing, human review, fabrication, bring-up,
  EMC, and physical measurements remain external

Open `project/{example.design_id}.kicad_pro` in KiCad. `requirements.json` is the
human/version-control input, `project/design.pcbir.json` is authoritative semantic
state, `validation/validation.json` is the full L0–L7 evidence report, and
`previews/` contains real KiCad exports.

Reproduce or verify all product examples from the repository root:

```bash
uv run python scripts/generate-product-examples.py
```
"""


def generate(output_root: Path, example: Example) -> dict[str, object]:
    spec = requirements(example)
    design = compile_requirements(spec)
    with tempfile.TemporaryDirectory(
        prefix=f".{example.directory}.building-", dir=output_root
    ) as temporary:
        staging = Path(temporary) / example.directory
        make_directory(staging)
        atomic_write_json(staging / "requirements.json", spec.to_dict())
        generated = generate_managed_project(spec, staging / "project")
        validation = validate_managed_project(
            generated.project,
            output=staging / "validation",
            timeout=180,
        )
        # The validation report and raw KiCad evidence are path-independent and
        # durable. The execution receipt intentionally contains the invoking
        # machine's absolute staging path, so examples do not publish it.
        (staging / "validation" / "receipt.json").unlink()
        if not validation.candidate_ready or validation.production_ready:
            raise RuntimeError(
                f"example did not reach the honest candidate state: {example.profile_id}"
            )
        preview = generate_previews(
            generated.project,
            staging / "previews",
            timeout=180,
        )
        normalize_preview_svgs(staging / "previews")
        summary: dict[str, object] = {
            "schema": "copperwright-product-example-acceptance",
            "version": 1,
            "profile": example.profile_id,
            "design_content_hash": design.content_hash(),
            "candidate_ready": validation.candidate_ready,
            "production_ready": validation.production_ready,
            "production_claimed": False,
            "validation_report_sha256": validation.report_sha256,
            "levels": [
                {
                    "level": level.level,
                    "state": level.state,
                    "outcome": level.outcome,
                }
                for level in validation.levels
            ],
            "preview_design_content_hash": preview.design_content_hash,
            "project_files": {
                key: relative
                for key, relative in sorted(generated.project.manifest["files"].items())
            },
        }
        atomic_write_json(staging / "acceptance.json", summary)
        atomic_write_text(
            staging / "README.md",
            readme(example, len(design.components), len(design.constraints)),
        )
        os.replace(staging, output_root / example.directory)
    return summary


def main() -> int:
    output_root = make_directory(parse_args().output_root.expanduser().resolve())
    for example in EXAMPLES:
        target = output_root / example.directory
        if target.is_symlink():
            raise RuntimeError(f"example output is an unsafe symlink: {target}")
        summary = (
            verify_existing(target, example)
            if target.is_dir()
            else generate(output_root, example)
        )
        print(
            f"{example.profile_id}: candidate={summary['candidate_ready']} "
            f"production={summary['production_ready']} "
            f"hash={summary['design_content_hash']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
