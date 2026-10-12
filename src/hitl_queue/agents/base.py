from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import threading


@dataclass(frozen=True)
class AgentLimits:
    """Execution limits for an adapter.

    ``allowed_folders`` only validates the starting working directory. It is
    not a filesystem sandbox and does not prevent a CLI from accessing paths
    outside those folders. Configure the invoked CLI's own sandbox and
    permission flags when isolation is required.
    """

    allowed_folders: tuple[Path, ...]
    max_turns: int
    timeout_seconds: float
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not self.allowed_folders:
            raise ValueError("allowed_folders must contain at least one folder")
        if self.max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be at least 1")


@dataclass(frozen=True)
class AgentResult:
    output: str
    ok: bool
    error: str | None
    duration: float
    turns: int | None
    truncated: bool = False


class Agent(ABC):
    def __init__(
        self,
        name: str,
        limits: AgentLimits,
        *,
        work_dir: str | Path | None = None,
        allow_agent_to_agent: bool = False,
    ) -> None:
        if not name:
            raise ValueError("agent name cannot be empty")
        self.name = name
        self.limits = limits
        self.work_dir = Path(work_dir).expanduser().resolve() if work_dir is not None else None
        self.allow_agent_to_agent = allow_agent_to_agent

    def run(
        self, prompt: str, cwd: str | Path, *, cancel_event: threading.Event | None = None
    ) -> AgentResult:
        folder = Path(cwd).expanduser().resolve()
        if not any(folder.is_relative_to(allowed.resolve()) for allowed in self.limits.allowed_folders):
            return AgentResult(
                output="",
                ok=False,
                error=f"cwd is outside allowed_folders: {folder}",
                duration=0.0,
                turns=0,
            )
        event = cancel_event or threading.Event()
        if event.is_set():
            return AgentResult("", False, "cancelled", 0.0, 0)
        result = self._run(prompt, folder, event)
        if result.turns is not None and result.turns > self.limits.max_turns:
            return AgentResult(
                output=result.output,
                ok=False,
                error=(
                    f"agent used {result.turns} turns; "
                    f"limit is {self.limits.max_turns}"
                ),
                duration=result.duration,
                turns=result.turns,
                truncated=result.truncated,
            )
        return result

    def cancel(self, cancel_event: threading.Event) -> None:
        """Ask an in-flight run to stop. Adapters override when applicable."""
        cancel_event.set()

    @abstractmethod
    def _run(self, prompt: str, cwd: Path, cancel_event: threading.Event) -> AgentResult:
        """Run the adapter after common safety checks."""
