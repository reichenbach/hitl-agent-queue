from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentLimits:
    allowed_folders: tuple[Path, ...]
    max_turns: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.allowed_folders:
            raise ValueError("allowed_folders must contain at least one folder")
        if self.max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")


@dataclass(frozen=True)
class AgentResult:
    output: str
    ok: bool
    error: str | None
    duration: float
    turns: int


class Agent(ABC):
    def __init__(self, name: str, limits: AgentLimits) -> None:
        if not name:
            raise ValueError("agent name cannot be empty")
        self.name = name
        self.limits = limits

    def run(self, prompt: str, cwd: str | Path) -> AgentResult:
        folder = Path(cwd).expanduser().resolve()
        if not any(folder.is_relative_to(allowed.resolve()) for allowed in self.limits.allowed_folders):
            return AgentResult(
                output="",
                ok=False,
                error=f"cwd is outside allowed_folders: {folder}",
                duration=0.0,
                turns=0,
            )
        result = self._run(prompt, folder)
        if result.turns > self.limits.max_turns:
            return AgentResult(
                output=result.output,
                ok=False,
                error=(
                    f"agent used {result.turns} turns; "
                    f"limit is {self.limits.max_turns}"
                ),
                duration=result.duration,
                turns=result.turns,
            )
        return result

    @abstractmethod
    def _run(self, prompt: str, cwd: Path) -> AgentResult:
        """Run the adapter after common safety checks."""
