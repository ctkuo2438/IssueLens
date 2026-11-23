"""Private, versioned execution state for PostgreSQL-backed analyses."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from openai.types.responses import ResponseFunctionToolCall
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from backend.app.context import ContextCheckpoint
from backend.app.github import GitHubToolState
from backend.app.models import AnalysisResult


class PendingTools(BaseModel):
    calls: list[ResponseFunctionToolCall]
    required_action: str | None
    closing_for_repairs: bool
    at_limit: bool


class ScheduledRetry(BaseModel):
    reason: str
    not_before: AwareDatetime
    attempt_id: int | None = None


class AgentCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    signature: str
    started_at: AwareDatetime
    result: AnalysisResult
    history: list[dict[str, Any]] = Field(default_factory=list)
    context: ContextCheckpoint = Field(default_factory=ContextCheckpoint)
    tools: GitHubToolState | None = None
    repair_feedback: str | None = None
    pending_tools: PendingTools | None = None
    in_flight: Literal["model", "tool"] | None = None
    retry: ScheduledRetry | None = None
    finished: bool = False

    @classmethod
    def start(cls, signature: str) -> "AgentCheckpoint":
        started_at = datetime.now(UTC)
        return cls(signature=signature, started_at=started_at, result=AnalysisResult(analysis_date=started_at.date()))

    def elapsed_seconds(self) -> float:
        return max(0, (datetime.now(UTC) - self.started_at).total_seconds())


CheckpointWriter = Callable[[AgentCheckpoint], Awaitable[None]]
