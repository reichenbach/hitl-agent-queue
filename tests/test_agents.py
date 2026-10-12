from __future__ import annotations

import json
import sys
import tempfile
import unittest
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

    def read(self) -> bytes:
        return self.payload


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
allowed_folders = ["work"]
max_turns = 3
timeout_seconds = 4.5

[agents.server]
type = "openai_compat"
base_url = "http://localhost:1234/"
model = "local-model"
api_key_env = "TEST_AGENT_KEY"
allowed_folders = ["."]
max_turns = 1
timeout_seconds = 10
"""
        )
        agents = load_agents(config)
        self.assertEqual(set(agents), {"worker", "server"})
        self.assertIsInstance(agents["worker"], CliAgent)
        self.assertEqual(agents["worker"].limits.allowed_folders, (self.root / "work",))
        self.assertEqual(agents["server"].base_url, "http://localhost:1234")

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

    def test_cli_adapter_captures_output_and_has_devnull_stdin(self):
        script = "import sys; print(sys.argv[1]); print(sys.stdin.read() == '')"
        agent = CliAgent("cli", [sys.executable, "-c", script], self.limits)
        result = agent.run("the prompt", self.root)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output.splitlines(), ["the prompt", "True"])
        self.assertEqual(result.turns, 1)

    def test_cli_timeout(self):
        limits = AgentLimits((self.root,), max_turns=1, timeout_seconds=0.05)
        agent = CliAgent(
            "slow", [sys.executable, "-c", "import time; time.sleep(2)"], limits
        )
        result = agent.run("ignored", self.root)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        self.assertLess(result.duration, 1)

    @mock.patch("urllib.request.urlopen")
    def test_http_adapter_posts_chat_completion(self, urlopen):
        urlopen.return_value = _Response(
            {"choices": [{"message": {"content": "local answer"}}]}
        )
        agent = OpenAICompatAgent(
            "local", "http://llm-box:8000/", "test-model", self.limits
        )
        result = agent.run("say hi", self.root)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output, "local answer")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://llm-box:8000/v1/chat/completions")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2)
        self.assertEqual(
            json.loads(request.data),
            {"model": "test-model", "messages": [{"role": "user", "content": "say hi"}]},
        )

    @mock.patch("urllib.request.urlopen")
    def test_http_adapter_requires_configured_key(self, urlopen):
        agent = OpenAICompatAgent(
            "local", "http://localhost:1234", "model", self.limits, "MISSING_TEST_KEY"
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            result = agent.run("hello", self.root)
        self.assertFalse(result.ok)
        self.assertEqual(result.turns, 0)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
