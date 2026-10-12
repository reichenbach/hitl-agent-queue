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
    ) -> None:
        super().__init__(name, limits)
        self.result = AgentResult(output, ok, error, duration, turns)
        self.calls: list[tuple[str, Path]] = []
        self.delay_seconds = delay_seconds
        self._cancelled = threading.Event()

    def _run(self, prompt: str, cwd: Path) -> AgentResult:
        self._cancelled.clear()
        self.calls.append((prompt, cwd))
        if self.delay_seconds and self._cancelled.wait(self.delay_seconds):
            return AgentResult("", False, "cancelled", 0.0, 0)
        return self.result

    def cancel(self) -> None:
        self._cancelled.set()
