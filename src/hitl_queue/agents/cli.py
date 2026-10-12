from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Sequence

from .base import Agent, AgentLimits, AgentResult


class CliAgent(Agent):
    """Run an agent CLI, appending the prompt as its final argument."""

    def __init__(self, name: str, command: Sequence[str], limits: AgentLimits) -> None:
        super().__init__(name, limits)
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must be a non-empty list of strings")
        self.command = tuple(command)

    def _run(self, prompt: str, cwd: Path) -> AgentResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [*self.command, prompt],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.limits.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = _timeout_output(exc)
            return AgentResult(
                output=output,
                ok=False,
                error=f"timed out after {self.limits.timeout_seconds:g} seconds",
                duration=time.monotonic() - started,
                turns=1,
            )
        except OSError as exc:
            return AgentResult("", False, str(exc), time.monotonic() - started, 1)

        stderr = completed.stderr.strip()
        return AgentResult(
            output=completed.stdout,
            ok=completed.returncode == 0,
            error=None if completed.returncode == 0 else stderr or f"exit code {completed.returncode}",
            duration=time.monotonic() - started,
            turns=1,
        )


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    value = exc.stdout or ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
