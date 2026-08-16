"""A deterministic first-board path that works without a model account."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import make_directory
from pcbdraft.core.repository import current_repository
from pcbdraft.core.resources import data_path
from pcbdraft.core.runs import new_run_id
from pcbdraft.services.managed import generate_managed_project
from pcbdraft.verification.validation import validate_managed_project

DEMO_SENTENCE = "做一块 3.3V 的温度传感器小板，带状态灯和 I2C 接口"


def _validate_demo_request(value: str) -> str:
    request = " ".join(value.split())
    if not request or len(request.encode("utf-8")) > 4096:
        raise ValidationError("demo request must be between 1 byte and 4 KiB")
    lowered = request.casefold()
    temperature = any(token in lowered for token in ("temperature", "temp", "温度"))
    sensor = any(token in lowered for token in ("sensor", "传感器"))
    if not temperature or not sensor:
        raise ValidationError(
            "the offline first-board demo is intentionally fixed to a temperature "
            f"sensor reference design; try: {DEMO_SENTENCE}"
        )
    return request


def _default_output() -> Path:
    projects = make_directory(current_repository().projects_root)
    base = projects / "temperature-sensor-demo"
    if not base.exists() and not base.is_symlink():
        return base
    suffix = re.sub(r"[^0-9A-Za-z]+", "-", new_run_id()).strip("-").casefold()
    return projects / f"temperature-sensor-demo-{suffix}"


def run_first_board_demo(
    request: str,
    *,
    output: str | Path | None = None,
    validate: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Generate the disclosed reference board from one bounded sentence."""

    clean_request = _validate_demo_request(request)
    target = (
        Path(output).expanduser().resolve(strict=False)
        if output is not None
        else _default_output()
    )
    if target.name in {"", ".", ".."} or target.exists() or target.is_symlink():
        raise ValidationError("demo output must be a new, safe directory")
    make_directory(target.parent)
    generated = generate_managed_project(
        data_path("benchmark", "acceptance_requirements.json"), target
    )
    validation = (
        validate_managed_project(generated.project, timeout=timeout)
        if validate
        else None
    )
    return {
        "request": clean_request,
        "mapping": (
            "offline sentence matched the bundled, deterministic ATtiny/TMP102 "
            "temperature-sensor reference design"
        ),
        "project": str(generated.project.root),
        "schematic": str(generated.project.schematic_path),
        "board": str(generated.project.board_path),
        "kicad_project": str(generated.project.project_path),
        "validation": (
            {
                "candidate_ready": validation.candidate_ready,
                "report": str(validation.report_path),
            }
            if validation is not None
            else {"candidate_ready": None, "report": None}
        ),
    }
