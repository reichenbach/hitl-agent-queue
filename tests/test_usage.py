from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hitl_queue import Queue
from hitl_queue.agents import AgentLimits, FakeAgent, UsagePlan


class Clock:
    def __init__(self, value: float = 1_700_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class Notifier:
    def __init__(self):
        self.calls = []

    def notify(self, text, *, source="hitl-queue"):
        self.calls.append((text, source))
        return {"ok": True, "channel": "fake"}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.clock = Clock()
        self.notifier = Notifier()
        self.queue = Queue(self.root / "state", usage_clock=self.clock, notifier=self.notifier)
        self.limits = AgentLimits((self.work,), 2, 10)

    def tearDown(self):
        self.queue.close()
        self.tmp.cleanup()

    def agent(self, name="Alpha", *, plan=None, duration=60, usage=None):
        return FakeAgent(name, self.limits, output="done", duration=duration, usage=usage,
                         usage_plan=plan, work_dir=self.work)

    def run_agent(self, name="Alpha"):
        thread = self.queue.threads.create("usage")
        return self.queue.threads.post(thread["id"], "user", f"@{name} task: work")

    def test_records_run_and_common_usage_fields(self):
        plan = UsagePlan("daily", runs=10, minutes=10, tokens=100)
        agent = self.agent(plan=plan, usage={"prompt_tokens": 20, "completion_tokens": 30})
        self.queue.threads.configure({"Alpha": agent})

        self.run_agent()

        item = self.queue.usage.summary("alpha")
        self.assertEqual(item["used"], {"runs": 1, "minutes": 1.0, "tokens": 50})
        self.assertEqual(item["percent_used"], {"runs": 10.0, "minutes": 10.0, "tokens": 50.0})
        row = self.queue.db.execute("SELECT ok,tokens,usage_json FROM usage_runs").fetchone()
        self.assertEqual((row["ok"], row["tokens"]), (1, 50))

    def test_unknown_tokens_stay_none(self):
        agent = self.agent(plan=UsagePlan("daily", tokens=100))
        self.queue.threads.configure({"Alpha": agent})
        self.run_agent()
        item = self.queue.usage.summary("Alpha")
        self.assertIsNone(item["used"]["tokens"])
        self.assertIsNone(item["percent_used"]["tokens"])
        self.assertFalse(item["blocked"])

    def test_warns_and_blocks_once_per_threshold_and_window(self):
        agent = self.agent(plan=UsagePlan("daily", runs=2, warn_threshold=.5))
        self.queue.threads.configure({"Alpha": agent})

        self.run_agent()
        self.run_agent()
        blocked = self.run_agent()

        self.assertEqual(blocked["replies"], [])
        events = [entry["event"] for entry in self.queue.threads.audit_log()]
        self.assertEqual(events.count("usage_warn"), 1)
        self.assertEqual(events.count("usage_blocked"), 1)
        self.assertEqual(len(self.notifier.calls), 2)
        self.assertTrue(all(source == "hitl-usage" for _, source in self.notifier.calls))

        self.clock.value += 86400
        self.run_agent()
        self.assertEqual(len(agent.calls), 3)
        events = [entry["event"] for entry in self.queue.threads.audit_log()]
        self.assertEqual(events.count("usage_warn"), 2)

    def test_mute_is_respected(self):
        self.queue.cfg["mute_notifications"] = True
        agent = self.agent(plan=UsagePlan("daily", runs=1))
        self.queue.threads.configure({"Alpha": agent})
        self.run_agent()
        self.assertEqual(self.notifier.calls, [])
        self.assertIn("usage_blocked", [row["event"] for row in self.queue.threads.audit_log()])

    def test_router_skips_controls_and_uses_quota_headroom_then_name(self):
        plan = UsagePlan("daily", runs=4)
        alpha = self.agent("Alpha", plan=plan)
        beta = self.agent("Beta", plan=plan)
        gamma = self.agent("Gamma", plan=plan)
        self.queue.threads.configure({a.name: a for a in (alpha, beta, gamma)})
        self.run_agent("Alpha")
        self.queue.threads.pause("Gamma")

        choice = self.queue.threads.route("next task", ["Gamma", "Beta", "Alpha"])

        self.assertEqual(choice["agent"], "Beta")
        self.assertEqual(choice["skipped"]["Gamma"], "paused")
        self.assertIn("100.0% quota headroom", choice["explanation"])


if __name__ == "__main__":
    unittest.main()
