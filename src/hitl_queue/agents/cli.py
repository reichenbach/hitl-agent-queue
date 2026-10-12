from __future__ import annotations

import os
import signal
import subprocess
import threading
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
        work_dir: str | Path | None = None,
        allow_agent_to_agent: bool = False,
    ) -> None:
        super().__init__(name, limits, work_dir=work_dir, allow_agent_to_agent=allow_agent_to_agent)
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must be a non-empty list of strings")
        self.command = tuple(command)
        if not all(isinstance(part, str) and part for part in extra_args):
            raise ValueError("extra_args must be a list of non-empty strings")
        if turns_flag is not None and not turns_flag:
            raise ValueError("turns_flag must be a non-empty string")
        if not isinstance(prompt_via, str) or prompt_via not in {"stdin", "arg"}:
            raise ValueError("prompt_via must be 'stdin' or 'arg'")
        if not all(isinstance(name, str) and name for name in env_passthrough):
            raise ValueError("env_passthrough must be a list of non-empty strings")
        self.extra_args = tuple(extra_args)
        self.turns_flag = turns_flag
        self.prompt_via = prompt_via
        self.env_passthrough = tuple(env_passthrough)
        self._process_lock = threading.Lock()
        self._processes: dict[threading.Event, subprocess.Popen[bytes]] = {}

    def cancel(self, cancel_event: threading.Event) -> None:
        with self._process_lock:
            cancel_event.set()
            process = self._processes.get(cancel_event)
        if process is not None:
            _terminate_process_group(process)

    def _run(self, prompt: str, cwd: Path, cancel_event: threading.Event) -> AgentResult:
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
        process = None
        workers: tuple[threading.Thread, ...] = ()
        stdout_capture = _BoundedCapture(self.limits.max_output_bytes)
        stderr_capture = _BoundedCapture(self.limits.max_output_bytes)
        try:
            with self._process_lock:
                if cancel_event.is_set():
                    return AgentResult("", False, "cancelled", time.monotonic() - started, 0)
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.PIPE if self.prompt_via == "stdin" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            with self._process_lock:
                self._processes[cancel_event] = process
                cancelled = cancel_event.is_set()
            if cancelled:
                _terminate_process_group(process)
                return AgentResult("", False, "cancelled", time.monotonic() - started, 0)
            assert process.stdout is not None and process.stderr is not None
            readers = (
                threading.Thread(target=stdout_capture.drain, args=(process.stdout,)),
                threading.Thread(target=stderr_capture.drain, args=(process.stderr,)),
            )
            workers = readers
            if process.stdin is not None:
                workers += (
                    threading.Thread(
                        target=_write_input, args=(process.stdin, input_bytes or b"")
                    ),
                )
            for worker in workers:
                worker.start()
            timed_out = False
            try:
                process.wait(timeout=self.limits.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
        except OSError as exc:
            return AgentResult("", False, str(exc), time.monotonic() - started, None)
        finally:
            if process is not None:
                _terminate_process_group(process)
            with self._process_lock:
                if self._processes.get(cancel_event) is process:
                    del self._processes[cancel_event]
            for worker in workers:
                worker.join()
        output, truncated = stdout_capture.result()
        if timed_out:
            return AgentResult(
                output, False,
                f"timed out after {self.limits.timeout_seconds:g} seconds",
                time.monotonic() - started, None, truncated,
            )
        error_output, _ = stderr_capture.result()
        return AgentResult(
            output=output,
            ok=process.returncode == 0,
            error=(
                None
                if process.returncode == 0
                else error_output.strip() or f"exit code {process.returncode}"
            ),
            duration=time.monotonic() - started,
            turns=None,
            truncated=truncated,
        )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def drain(self, handle) -> None:
        with handle:
            while chunk := handle.read(65536):
                remaining = self.limit - len(self.data)
                if remaining:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True

    def result(self) -> tuple[str, bool]:
        return self.data.decode("utf-8", errors="ignore"), self.truncated


def _write_input(handle, data: bytes) -> None:
    with handle:
        try:
            handle.write(data)
        except BrokenPipeError:
            pass
