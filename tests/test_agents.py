from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from hitl_queue.agents import (
    AgentLimits,
    CliAgent,
    FakeAgent,
    OpenAICompatAgent,
    load_agents,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1) -> bytes:
        if not self.payload:
            return b""
        if size < 0:
            size = len(self.payload)
        chunk, self.payload = self.payload[:size], self.payload[size:]
        return chunk

    def close(self) -> None:
        pass


class _SlowResponse(_Response):
    def __init__(self) -> None:
        super().__init__({"choices": [{"message": {"content": "too late"}}]})
        self.read_started = threading.Event()
        self.closed = threading.Event()

    def read(self, size=-1) -> bytes:
        self.read_started.set()
        self.closed.wait(2)
        if self.closed.is_set():
            raise OSError("response closed")
        return super().read(size)

    def close(self) -> None:
        self.closed.set()


class _ReadTimeoutResponse(_Response):
    def __init__(self) -> None:
        super().__init__({})
        self.read_calls = 0

    def read(self, size=-1) -> bytes:
        self.read_calls += 1
        raise socket.timeout("slow read")


class _Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        return self.response


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.limits = AgentLimits((self.root,), max_turns=2, timeout_seconds=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_loading_and_relative_allowed_folder(self):
        (self.root / "work").mkdir()
        config = self.root / "agents.toml"
        config.write_text(
            """
[agents.worker]
type = "cli"
command = ["python", "-c", "print('ok')"]
extra_args = ["--safe"]
turns_flag = "--max-turns"
prompt_via = "arg"
env_passthrough = ["SPECIAL_TOKEN"]
allowed_folders = ["work"]
work_dir = "work"
max_turns = 3
timeout_seconds = 4.5
max_output_bytes = 1234

[agents.server]
type = "openai_compat"
base_url = "http://localhost:1234/"
model = "local-model"
api_key_env = "TEST_AGENT_KEY"
allowed_folders = ["."]
work_dir = "."
max_turns = 1
timeout_seconds = 10
"""
        )
        agents = load_agents(config)
        self.assertEqual(set(agents), {"worker", "server"})
        self.assertIsInstance(agents["worker"], CliAgent)
        self.assertEqual(agents["worker"].limits.allowed_folders, (self.root / "work",))
        self.assertEqual(agents["worker"].extra_args, ("--safe",))
        self.assertEqual(agents["worker"].turns_flag, "--max-turns")
        self.assertEqual(agents["worker"].limits.max_output_bytes, 1234)
        self.assertEqual(agents["server"].base_url, "http://localhost:1234")

    def test_threads_config_loads_review_round_cap(self):
        from hitl_queue import Queue

        (self.root / "work").mkdir()
        config = self.root / "agents.toml"
        config.write_text(
            "[threads]\nmax_review_rounds = 5\n"
            "[agents.worker]\ntype='fake'\nallowed_folders=['work']\n"
            "work_dir='work'\nmax_turns=1\ntimeout_seconds=1\n"
        )
        queue = Queue(self.root / "state")
        try:
            queue.threads.configure_from(config)
            self.assertEqual(queue.cfg["max_review_rounds"], 5)
        finally:
            queue.close()

    def test_threads_config_without_round_cap_preserves_queue_config(self):
        from hitl_queue import Queue

        (self.root / "work").mkdir()
        config = self.root / "agents.toml"
        config.write_text(
            "[agents.worker]\ntype='fake'\nallowed_folders=['work']\n"
            "work_dir='work'\nmax_turns=1\ntimeout_seconds=1\n"
        )
        queue = Queue(self.root / "state", config={"max_review_rounds": 7})
        try:
            queue.threads.configure_from(config)
            self.assertEqual(queue.cfg["max_review_rounds"], 7)
        finally:
            queue.close()

    def test_folder_enforcement(self):
        outside = self.root.parent / "not-allowed"
        result = FakeAgent("fake", self.limits).run("hello", outside)
        self.assertFalse(result.ok)
        self.assertIn("outside allowed_folders", result.error)
        self.assertEqual(result.turns, 0)

    def test_turn_limit(self):
        agent = FakeAgent("fake", self.limits, output="partial", turns=3)
        result = agent.run("hello", self.root)
        self.assertFalse(result.ok)
        self.assertIn("limit is 2", result.error)
        self.assertEqual(result.turns, 3)

    def test_fake_concurrent_runs_have_independent_cancellation(self):
        agent = FakeAgent("fake", self.limits, output="done", delay_seconds=0.1)
        cancelled = threading.Event()
        continuing = threading.Event()
        results = {}
        first = threading.Thread(
            target=lambda: results.update(first=agent.run("one", self.root, cancel_event=cancelled))
        )
        second = threading.Thread(
            target=lambda: results.update(second=agent.run("two", self.root, cancel_event=continuing))
        )
        first.start()
        second.start()
        agent.cancel(cancelled)
        first.join()
        second.join()
        self.assertFalse(results["first"].ok)
        self.assertEqual(results["first"].error, "cancelled")
        self.assertTrue(results["second"].ok)

    def test_cli_arg_prompt_is_delimited_and_has_devnull_stdin(self):
        script = "import sys; print(sys.argv[1:]); print(sys.stdin.read() == '')"
        agent = CliAgent("cli", [sys.executable, "-c", script], self.limits, prompt_via="arg")
        result = agent.run("the prompt", self.root)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output.splitlines(), ["['--', 'the prompt']", "True"])
        self.assertIsNone(result.turns)

    def test_cli_stdin_extra_args_turns_and_environment_allowlist(self):
        script = (
            "import os,sys; print(sys.argv[1:]); print(sys.stdin.read()); "
            "print(os.getenv('KEPT')); print(os.getenv('DROPPED'))"
        )
        agent = CliAgent(
            "cli",
            [sys.executable, "-c", script],
            self.limits,
            extra_args=["extra"],
            turns_flag="--max-turns",
            env_passthrough=["KEPT"],
        )
        with mock.patch.dict(os.environ, {"KEPT": "yes", "DROPPED": "no"}, clear=False):
            result = agent.run("prompt on stdin", self.root)
        self.assertEqual(
            result.output.splitlines(),
            ["['extra', '--max-turns', '2']", "prompt on stdin", "yes", "None"],
        )

    def test_cli_timeout(self):
        limits = AgentLimits((self.root,), max_turns=1, timeout_seconds=0.05)
        agent = CliAgent(
            "slow", [sys.executable, "-c", "import time; time.sleep(2)"], limits
        )
        result = agent.run("ignored", self.root)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        self.assertLess(result.duration, 1)

    @unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
    def test_cli_timeout_kills_grandchild(self):
        pid_file = self.root / "grandchild.pid"
        grandchild = (
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)"
        )
        parent = (
            "import pathlib,subprocess,sys,time; "
            f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(30)"
        )
        limits = AgentLimits((self.root,), max_turns=1, timeout_seconds=0.2)
        result = CliAgent("tree", [sys.executable, "-c", parent], limits).run("x", self.root)
        self.assertFalse(result.ok)
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 1
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                self.fail(f"grandchild process {pid} still exists")
            time.sleep(0.02)

    def test_cli_output_is_capped(self):
        limits = AgentLimits((self.root,), 1, 2, max_output_bytes=5)
        agent = CliAgent("noisy", [sys.executable, "-c", "print('abcdefghij', end='')"], limits)
        result = agent.run("", self.root)
        self.assertEqual(result.output, "abcde")
        self.assertTrue(result.truncated)

    def test_cli_rejects_non_string_prompt_via(self):
        for value in (None, ["stdin"], {"stdin": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CliAgent("bad", [sys.executable], self.limits, prompt_via=value)

    @unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
    def test_cli_normal_exit_kills_grandchild(self):
        pid_file = self.root / "normal-grandchild.pid"
        child = "import time; time.sleep(30)"
        parent = (
            "import pathlib,subprocess,sys; "
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
        )
        result = CliAgent("tree", [sys.executable, "-c", parent], self.limits).run(
            "x", self.root
        )
        self.assertTrue(result.ok, result.error)
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    @mock.patch("urllib.request.build_opener")
    def test_http_adapter_posts_chat_completion(self, build_opener):
        opener = _Opener(_Response(
            {"choices": [{"message": {"content": "local answer"}}]}
        ))
        build_opener.return_value = opener
        agent = OpenAICompatAgent(
            "local", "http://192.168.1.20:8000/", "test-model", self.limits
        )
        result = agent.run("say hi", self.root)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "local answer")
        request, kwargs = opener.calls[0]
        self.assertEqual(request.full_url, "http://192.168.1.20:8000/v1/chat/completions")
        self.assertAlmostEqual(
            kwargs["timeout"], self.limits.timeout_seconds, delta=0.1
        )
        self.assertNotEqual(kwargs["timeout"], 0.25)
        self.assertEqual(
            json.loads(request.data),
            {"model": "test-model", "messages": [{"role": "user", "content": "say hi"}]},
        )
        proxy_handler, redirect_handler = build_opener.call_args.args
        self.assertIsInstance(proxy_handler, urllib.request.ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})
        self.assertEqual(redirect_handler.__class__.__name__, "_NoRedirect")

    @mock.patch("urllib.request.build_opener")
    def test_http_adapter_cancel_closes_slow_response(self, build_opener):
        response = _SlowResponse()
        build_opener.return_value = _Opener(response)
        agent = OpenAICompatAgent(
            "local", "http://localhost:1234", "model", self.limits
        )
        cancel_event = threading.Event()
        result = {}
        worker = threading.Thread(
            target=lambda: result.update(
                value=agent.run("hello", self.root, cancel_event=cancel_event)
            )
        )
        worker.start()
        self.assertTrue(response.read_started.wait(1))
        cancel_event.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(response.closed.is_set())
        self.assertFalse(result["value"].ok)
        self.assertEqual(result["value"].error, "cancelled")

    @mock.patch("urllib.request.build_opener")
    def test_http_adapter_does_not_retry_after_read_timeout(self, build_opener):
        response = _ReadTimeoutResponse()
        build_opener.return_value = _Opener(response)
        result = OpenAICompatAgent(
            "local", "http://localhost:1234", "model", self.limits
        ).run("hello", self.root)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timed out")
        self.assertEqual(response.read_calls, 1)

    @mock.patch("urllib.request.build_opener")
    def test_http_adapter_reports_http_protocol_errors(self, build_opener):
        errors = (
            http.client.BadStatusLine("broken status"),
            http.client.IncompleteRead(b"partial", 4),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                opener = mock.Mock()
                opener.open.side_effect = error
                build_opener.return_value = opener

                result = OpenAICompatAgent(
                    "local", "http://localhost:1234", "model", self.limits
                ).run("hello", self.root)

                self.assertFalse(result.ok)
                self.assertEqual(result.error, str(error))
                self.assertNotEqual(result.error, "timed out")

    def test_cli_termination_uses_session_leader_pid_as_process_group(self):
        from hitl_queue.agents.cli import _terminate_process_group

        process = mock.Mock(pid=4321)
        process.wait.side_effect = [subprocess.TimeoutExpired("test", 1), 0]
        with mock.patch("hitl_queue.agents.cli.os.killpg") as killpg, mock.patch(
            "hitl_queue.agents.cli.os.getpgid", create=True
        ) as getpgid:
            _terminate_process_group(process)
        getpgid.assert_not_called()
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(4321, 15), mock.call(4321, 9)],
        )

    @mock.patch("urllib.request.build_opener")
    def test_http_adapter_requires_configured_key(self, build_opener):
        agent = OpenAICompatAgent(
            "local", "http://localhost:1234", "model", self.limits, "MISSING_TEST_KEY"
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            result = agent.run("hello", self.root)
        self.assertFalse(result.ok)
        self.assertEqual(result.turns, 0)
        build_opener.assert_not_called()

    def test_http_rejects_insecure_public_host(self):
        with self.assertRaisesRegex(ValueError, "must use https"):
            OpenAICompatAgent("remote", "http://example.com", "model", self.limits)

    def test_http_allows_local_http_hosts(self):
        for url in (
            "http://localhost:1",
            "http://127.0.0.1",
            "http://10.0.0.2",
            "http://169.254.169.254",
            "http://box.local",
        ):
            OpenAICompatAgent("local", url, "model", self.limits)

    @mock.patch("urllib.request.build_opener")
    def test_http_output_is_capped(self, build_opener):
        build_opener.return_value = _Opener(
            _Response({"choices": [{"message": {"content": "abcdefghij"}}]})
        )
        limits = AgentLimits((self.root,), 1, 2, max_output_bytes=5)
        result = OpenAICompatAgent(
            "local", "http://localhost:1", "model", limits
        ).run("x", self.root)
        self.assertEqual(result.output, "")
        self.assertFalse(result.ok)
        self.assertTrue(result.truncated)

    def test_redirect_handler_disables_redirects(self):
        from hitl_queue.agents.openai_compat import _NoRedirect

        self.assertIsNone(
            _NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://example.com"
            )
        )


if __name__ == "__main__":
    unittest.main()
