from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence

from .base import Agent, AgentLimits, AgentResult


class CliAgent(Agent):
    """Run an agent CLI with a prompt supplied via stdin or a final argument."""

    def __init__(
        self,
        name: str,
        command: Sequence[str],
        limits: AgentLimits,
        *,
        extra_args: Sequence[str] = (),
        turns_flag: str | None = None,
        prompt_via: str = "stdin",
        env_passthrough: Sequence[str] = (),
    ) -> None:
        super().__init__(name, limits)
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must be a non-empty list of strings")
        self.command = tuple(command)
        if not all(isinstance(part, str) and part for part in extra_args):
            raise ValueError("extra_args must be a list of non-empty strings")
        if turns_flag is not None and not turns_flag:
            raise ValueError("turns_flag must be a non-empty string")
        if prompt_via not in {"stdin", "arg"}:
            raise ValueError("prompt_via must be 'stdin' or 'arg'")
        if not all(isinstance(name, str) and name for name in env_passthrough):
            raise ValueError("env_passthrough must be a list of non-empty strings")
        self.extra_args = tuple(extra_args)
        self.turns_flag = turns_flag
        self.prompt_via = prompt_via
        self.env_passthrough = tuple(env_passthrough)

    def _run(self, prompt: str, cwd: Path) -> AgentResult:
        started = time.monotonic()
        argv = [*self.command, *self.extra_args]
        if self.turns_flag:
            argv.extend((self.turns_flag, str(self.limits.max_turns)))
        input_bytes = prompt.encode() if self.prompt_via == "stdin" else None
        if self.prompt_via == "arg":
            argv.extend(("--", prompt))
        allowed_env = {"PATH", "HOME", "LANG", "TERM", "USER", "TMPDIR"}
        allowed_env.update(self.env_passthrough)
        env = {key: value for key, value in os.environ.items() if key in allowed_env}
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.PIPE if self.prompt_via == "stdin" else subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=env,
                    start_new_session=True,
                )
                try:
                    process.communicate(input=input_bytes, timeout=self.limits.timeout_seconds)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(process)
                    output, truncated = _read_limited(stdout, self.limits.max_output_bytes)
                    return AgentResult(
                        output, False,
                        f"timed out after {self.limits.timeout_seconds:g} seconds",
                        time.monotonic() - started, None, truncated,
                    )
                output, truncated = _read_limited(stdout, self.limits.max_output_bytes)
                error_output, _ = _read_limited(stderr, self.limits.max_output_bytes)
                return AgentResult(
                    output=output,
                    ok=process.returncode == 0,
                    error=None if process.returncode == 0 else error_output.strip() or f"exit code {process.returncode}",
                    duration=time.monotonic() - started,
                    turns=None,
                    truncated=truncated,
                )
        except OSError as exc:
            return AgentResult("", False, str(exc), time.monotonic() - started, None)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _read_limited(handle, limit: int) -> tuple[str, bool]:
    handle.seek(0)
    data = handle.read(limit + 1)
    return data[:limit].decode("utf-8", errors="ignore"), len(data) > limit
