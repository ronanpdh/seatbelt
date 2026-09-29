import tomllib
from importlib.metadata import version
from pathlib import Path

import seatbelt

PYPROJECT = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())


def test_version_is_read_from_the_seatbelt_ai_distribution():
    # `seatbelt` on PyPI is another project; ours is `seatbelt-ai`, holding the `seatbelt` module
    assert PYPROJECT["project"]["name"] == "seatbelt-ai"
    assert seatbelt.__version__ == PYPROJECT["project"]["version"]
    assert version("seatbelt-ai") == PYPROJECT["project"]["version"]


def test_the_wheel_holds_the_seatbelt_module_and_command():
    assert PYPROJECT["tool"]["uv"]["build-backend"]["module-name"] == "seatbelt"
    assert PYPROJECT["project"]["scripts"] == {"seatbelt": "seatbelt.cli:app"}
