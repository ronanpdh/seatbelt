"""Write the JSON schemas for the scenario file and the findings report to docs/schema/."""

import json
from pathlib import Path

from seatbelt.scenarios.model import Scenario
from seatbelt.scenarios.runner import Report

OUT = Path(__file__).resolve().parent.parent / "docs" / "schema"
SCHEMAS = {"scenario.json": Scenario, "findings.json": Report}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMAS.items():
        (OUT / name).write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
        print(f"wrote {OUT / name}")
