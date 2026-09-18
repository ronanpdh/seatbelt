"""In-container entry point: record one scenario into /out. Started by sandbox.run_sandboxed."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from seatbelt.scenarios.model import Scenario
from seatbelt.scenarios.runner import Target, record

ENV = "SEATBELT_CHILD"
TARGET_DIR = "/target"
OUT_DIR = "/out"


class ChildSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Scenario
    target: str  # module:function, imported from TARGET_DIR
    metadata: dict[str, Any] = Field(default_factory=dict)  # run.start attrs; free-form JSON


def main(
    *,
    target_dir: str = TARGET_DIR,
    out_dir: str = OUT_DIR,
    env: Mapping[str, str] | None = None,
) -> int:
    # A bad spec or an unimportable target raises: the container exits non-zero with no
    # ledger and the host reports the stderr file. No try/except by design.
    spec = ChildSpec.model_validate_json((os.environ if env is None else env)[ENV])
    sys.path.insert(0, target_dir)
    module, _, attr = spec.target.partition(":")
    fn = getattr(importlib.import_module(module), attr)
    if not callable(fn):
        raise TypeError(f"{spec.target} is not callable")
    target = cast(Target, fn)  # the callable's signature cannot be checked at runtime
    record(spec.scenario, Path(out_dir), target, metadata=spec.metadata)
    return 0


if __name__ == "__main__":
    sys.exit(main())
