from __future__ import annotations

from pathlib import Path

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
    ) -> None:
        super().__init__(name, limits)
        self.result = AgentResult(output, ok, error, duration, turns)
        self.calls: list[tuple[str, Path]] = []

    def _run(self, prompt: str, cwd: Path) -> AgentResult:
        self.calls.append((prompt, cwd))
        return self.result
