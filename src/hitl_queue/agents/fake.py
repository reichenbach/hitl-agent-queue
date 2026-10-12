from __future__ import annotations

from pathlib import Path
import threading

from .base import Agent, AgentLimits, AgentResult


class FakeAgent(Agent):
    """Deterministic adapter for unit tests and local dry runs."""

    def __init__(
        self,
        name: str,
        limits: AgentLimits,
        output: str = "",
        ok: bool = True,
        error: str | None = None,
        duration: float = 0.0,
        turns: int = 1,
        delay_seconds: float = 0.0,
        work_dir: str | Path | None = None,
        allow_agent_to_agent: bool = False,
        usage_plan=None,
        usage=None,
    ) -> None:
        super().__init__(name, limits, work_dir=work_dir, allow_agent_to_agent=allow_agent_to_agent,
                         usage_plan=usage_plan)
        self.result = AgentResult(output, ok, error, duration, turns, usage=usage)
        self.calls: list[tuple[str, Path]] = []
        self.delay_seconds = delay_seconds

    def _run(self, prompt: str, cwd: Path, cancel_event: threading.Event) -> AgentResult:
        self.calls.append((prompt, cwd))
        if self.delay_seconds and cancel_event.wait(self.delay_seconds):
            return AgentResult("", False, "cancelled", 0.0, 0)
        return self.result
