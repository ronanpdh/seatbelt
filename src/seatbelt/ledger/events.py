"""Event model and canonical hashing for the ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

GENESIS_HASH = "0" * 64  # prev_hash of the first event in a run
SCHEMA_VERSION = 1  # bump on any change to Event fields or their meaning


class Kind(StrEnum):
    """What happened. Names follow the scope chain, not any one framework."""

    RUN_START = "run.start"
    USER_MESSAGE = "user.message"
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    POLICY_CHECK = "policy.check"
    DECISION = "decision"
    ACTION = "action"
    OUTCOME = "outcome"
    RUN_END = "run.end"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    MODEL = "model"
    TOOL = "tool"
    POLICY = "policy"
    SYSTEM = "system"


class Actor(BaseModel):
    """Who did it. `id` is whatever identifies them in the host system."""

    model_config = ConfigDict(extra="forbid")  # unknown keys would otherwise survive verification

    type: ActorType
    id: str
    version: str | None = None  # model version, tool version, agent build


class Event(BaseModel):
    """One immutable fact about an interaction.

    `attrs` keys should follow OpenTelemetry GenAI semantic conventions where one
    exists (gen_ai.request.model, gen_ai.usage.input_tokens, ...). Anything else
    is namespaced by the adapter that produced it.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int  # required, so pre-versioning ledgers fail loudly on read
    id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    seq: int
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Kind
    actor: Actor
    parent_id: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str = ""

    def canonical(self) -> bytes:
        """Deterministic bytes of everything except `hash` itself."""
        data = self.model_dump(mode="json", exclude={"hash"})
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    def sealed(self) -> Event:
        """Return a copy with `hash` filled in."""
        return self.model_copy(update={"hash": self.compute_hash()})
