from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .base import Agent, AgentLimits, AgentResult


class OpenAICompatAgent(Agent):
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        limits: AgentLimits,
        api_key_env: str | None = None,
    ) -> None:
        super().__init__(name, limits)
        if not base_url or not model:
            raise ValueError("base_url and model are required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env

    def _run(self, prompt: str, cwd: Path) -> AgentResult:
        started = time.monotonic()
        body = json.dumps(
            {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                return AgentResult(
                    "", False, f"environment variable {self.api_key_env} is not set",
                    time.monotonic() - started, 0,
                )
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.limits.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            output = payload["choices"][0]["message"]["content"]
            if not isinstance(output, str):
                raise ValueError("response content is not a string")
        except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            return AgentResult("", False, str(exc), time.monotonic() - started, 1)
        return AgentResult(output, True, None, time.monotonic() - started, 1)
