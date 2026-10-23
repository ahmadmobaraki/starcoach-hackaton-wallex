from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: AgentAction
    confidence: float = Field(ge=0.0, le=1.0)
    size_fraction: float = Field(ge=0.0, le=1.0)
    order_type: Literal["LIMIT", "MARKETABLE_LIMIT"] = "LIMIT"
    limit_offset_bps: float = Field(default=0.0, ge=0.0, le=100.0)
    take_profit_bps: float = Field(ge=0.0, le=6000.0)
    stop_loss_bps: float = Field(ge=0.0, le=2000.0)
    horizon_sec: int = Field(ge=60, le=2_592_000)
    regime: str = Field(default="unknown", max_length=40)
    rationale: str = Field(default="", max_length=600)
    risk_flags: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def _consistency(self) -> AgentDecision:
        if self.action == AgentAction.HOLD:
            if self.size_fraction > 0.0001:
                self.size_fraction = 0.0
            return self
        if self.confidence <= 0:
            raise ValueError("BUY/SELL requires confidence > 0")
        if self.size_fraction <= 0:
            raise ValueError("BUY/SELL requires size_fraction > 0")
        if self.stop_loss_bps <= 0:
            raise ValueError("BUY/SELL requires stop_loss_bps > 0")
        if self.take_profit_bps < self.stop_loss_bps:
            raise ValueError("take_profit_bps must be >= stop_loss_bps")
        return self

    @property
    def is_entry(self) -> bool:
        return self.action == AgentAction.BUY

    @property
    def is_exit(self) -> bool:
        return self.action == AgentAction.SELL

    def response_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class ReflectionNote(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    outcome: Literal["win", "loss", "neutral"] = "neutral"
    note: str = Field(min_length=1, max_length=1600)
    lesson: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence_calibration: str = Field(default="", max_length=500)
    suggested_adjustment: str = Field(default="", max_length=1000)
    avoid_patterns: list[str] = Field(default_factory=list, max_length=12)


class SessionReview(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=2000)
    lessons: list[str] = Field(default_factory=list, max_length=12)
    best_patterns: list[str] = Field(default_factory=list, max_length=12)
    avoid_patterns: list[str] = Field(default_factory=list, max_length=12)
    importance: float = Field(default=0.6, ge=0.0, le=1.0)


def parse_json_object(text: str) -> dict:
    """Extract a JSON object from a model response, tolerating code fences."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data
