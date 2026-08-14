#!/usr/bin/env python3
"""Boot PCBDraft's real DeepSeek Harness SDK composition without a model call."""

from __future__ import annotations

import json
import tempfile
from importlib.resources import as_file, files

from deepseek_harness import DeepSeekHarness


def main() -> int:
    resource = files("pcbdraft").joinpath("data/deepseek_provider.cordis.yml")
    with (
        tempfile.TemporaryDirectory() as temporary,
        as_file(resource) as config,
        DeepSeekHarness(
            provider="deepseek-official",
            model="deepseek-v4-flash",
            cwd=temporary,
            session_root=temporary,
            cordis=str(config),
            request_timeout_seconds=15,
        ),
    ):
        pass
    print(
        json.dumps(
            {
                "runtime": "deepseek-harness-sdk",
                "composition": "pcbdraft-provider",
                "model_called": False,
                "ok": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
