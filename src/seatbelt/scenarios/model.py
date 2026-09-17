"""Scenario schema: adversarial inputs plus the checks a safe agent must pass."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

OWASP_AGENTIC = {
    "ASI01": "Agent Goal Hijack",
    "ASI02": "Tool Misuse and Exploitation",
    "ASI03": "Identity and Privilege Abuse",
    "ASI04": "Agentic Supply Chain Vulnerabilities",
    "ASI05": "Unexpected Code Execution",
    "ASI06": "Context Management and Retrieval Manipulation",
    "ASI07": "Insecure Inter-Agent Communication",
    "ASI08": "Cascading Failures",
    "ASI09": "Human-Agent Trust Exploitation",
    "ASI10": "Rogue Agents",
}
_ATLAS = re.compile(r"AML\.T\d{4}(\.\d{3})?")


class ScenarioError(Exception):
    """A corpus file cannot be used. The message names the file."""


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class _Check(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoToolCall(_Check):
    no_tool_call: str


class ToolCallCheck(_Check):
    tool_call: str


class PolicyDeniedCheck(_Check):
    policy_denied: str


class NoMatch(_Check):
    no_match: str

    @field_validator("no_match")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"no_match is not a valid regex: {exc}") from exc
        return value


class RunOk(_Check):
    run_ok: bool


Check = NoToolCall | ToolCallCheck | PolicyDeniedCheck | NoMatch | RunOk


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    owasp: list[str] = Field(default_factory=list)  # empty only for a benign control
    atlas: list[str] = Field(default_factory=list)
    severity: Severity
    user_message: str
    tool_results: dict[str, str] = Field(default_factory=dict)
    checks: list[Check] = Field(min_length=1)

    @field_validator("owasp")
    @classmethod
    def _known_owasp(cls, ids: list[str]) -> list[str]:
        unknown = [i for i in ids if i not in OWASP_AGENTIC]
        if unknown:
            raise ValueError(f"unknown OWASP Agentic ids {unknown}")
        return ids

    @field_validator("atlas")
    @classmethod
    def _atlas_shape(cls, ids: list[str]) -> list[str]:
        bad = [i for i in ids if not _ATLAS.fullmatch(i)]
        if bad:
            raise ValueError(f"atlas ids must look like AML.T0051 or AML.T0051.001: {bad}")
        return ids


@dataclass(frozen=True)
class Inputs:
    """What the scenario feeds the target; poisoned text replaces declared tools' results."""

    user_message: str
    tool_results: dict[str, str] = field(default_factory=dict[str, str])

    def tool_result[T](self, name: str, real: T) -> T | str:
        return self.tool_results.get(name, real)


def load_scenario(path: Path) -> Scenario:
    try:
        data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        scenario = Scenario.model_validate(data)
    except (yaml.YAMLError, ValidationError, OSError, UnicodeDecodeError) as exc:
        raise ScenarioError(f"{path}: {exc}") from exc
    if scenario.id != path.stem:
        raise ScenarioError(f"{path}: id {scenario.id!r} does not match the filename")
    return scenario


def load_corpus(directory: Path) -> list[Scenario]:
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ScenarioError(f"{directory}: no scenarios (*.yaml) found")
    return [load_scenario(p) for p in paths]  # id == stem, so ids are unique by construction


def corpus_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.yaml")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
