from __future__ import annotations

import http.client
import ipaddress
import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import Agent, AgentLimits, AgentResult


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAICompatAgent(Agent):
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        limits: AgentLimits,
        api_key_env: str | None = None,
        *,
        work_dir: str | Path | None = None,
        allow_agent_to_agent: bool = False,
    ) -> None:
        super().__init__(name, limits, work_dir=work_dir, allow_agent_to_agent=allow_agent_to_agent)
        if not base_url or not model:
            raise ValueError("base_url and model are required")
        _validate_base_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env

    def _run(self, prompt: str, cwd: Path, cancel_event) -> AgentResult:
        started = time.monotonic()
        deadline = started + self.limits.timeout_seconds
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
        outcomes: queue.Queue[AgentResult] = queue.Queue(maxsize=1)
        response_lock = threading.Lock()
        current_response: list[object] = []

        def perform_request() -> None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect()
            )
            try:
                remaining = max(0.0, deadline - time.monotonic())
                with opener.open(request, timeout=remaining) as response:
                    with response_lock:
                        current_response.append(response)
                    result = self._read_response(response, started)
                outcomes.put(result)
            except (
                OSError, http.client.HTTPException, ValueError, KeyError, IndexError,
                TypeError, json.JSONDecodeError,
            ) as exc:
                outcomes.put(AgentResult("", False, str(exc), time.monotonic() - started, None))
            finally:
                with response_lock:
                    current_response.clear()

        worker = threading.Thread(target=perform_request, daemon=True)
        worker.start()
        while True:
            try:
                return outcomes.get_nowait()
            except queue.Empty:
                pass
            if cancel_event.is_set():
                with response_lock:
                    if current_response:
                        current_response[0].close()
                return AgentResult("", False, "cancelled", time.monotonic() - started, 0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with response_lock:
                    if current_response:
                        current_response[0].close()
                return AgentResult("", False, "timed out", time.monotonic() - started, None)
            cancel_event.wait(min(remaining, 0.05))

    def _read_response(self, response, started: float) -> AgentResult:
        try:
            chunks = []
            size = 0
            while size <= self.limits.max_output_bytes:
                try:
                    chunk = response.read(
                        min(65536, self.limits.max_output_bytes + 1 - size)
                    )
                except (socket.timeout, TimeoutError):
                    return AgentResult(
                        "", False, "timed out", time.monotonic() - started, None
                    )
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
            if len(raw) > self.limits.max_output_bytes:
                return AgentResult(
                    "", False, "response body exceeded max_output_bytes",
                    time.monotonic() - started, None, True,
                )
            payload = json.loads(raw.decode("utf-8"))
            output = payload["choices"][0]["message"]["content"]
            if not isinstance(output, str):
                raise ValueError("response content is not a string")
        except (
            OSError, http.client.HTTPException, ValueError, KeyError, IndexError,
            TypeError, json.JSONDecodeError,
        ) as exc:
            return AgentResult("", False, str(exc), time.monotonic() - started, None)
        encoded = output.encode("utf-8")
        truncated = len(encoded) > self.limits.max_output_bytes
        if truncated:
            output = encoded[:self.limits.max_output_bytes].decode("utf-8", errors="ignore")
        return AgentResult(output, True, None, time.monotonic() - started, None, truncated)


def _validate_base_url(base_url: str) -> None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute http(s) URL")
    if parsed.scheme == "https":
        return
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        return
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        raise ValueError("base_url must use https unless its host is local or a private address")
    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    if not (
        address.is_loopback
        or address.is_link_local
        or any(
            address.version == network.version and address in network
            for network in private_networks
        )
    ):
        raise ValueError("base_url must use https unless its host is local or a private address")
