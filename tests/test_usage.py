from __future__ import annotations

import tempfile
import threading
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

    def test_concurrent_posts_reserve_run_quota_before_agent_execution(self):
        agent = self.agent(plan=UsagePlan("daily", runs=1))
        self.queue.threads.configure({"worker-key": agent})
        thread = self.queue.threads.create("concurrent", max_agent_runs=10)
        barrier = threading.Barrier(6)
        results = []

        def post():
            barrier.wait()
            results.append(self.queue.threads.post(
                thread["id"], "user", "@worker-key task: work"
            ))

        workers = [threading.Thread(target=post) for _ in range(5)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(sum(bool(result["replies"]) for result in results), 1)
        self.assertEqual(self.queue.usage.summary("worker-key")["used"]["runs"], 1)

    def test_usage_is_keyed_by_config_key_not_agent_display_name(self):
        agent = self.agent("Display Name", plan=UsagePlan("daily", runs=1))
        self.queue.threads.configure({"worker-key": agent})
        thread = self.queue.threads.create("keyed")

        first = self.queue.threads.post(thread["id"], "user", "@worker-key task: one")
        second = self.queue.threads.post(thread["id"], "user", "@worker-key task: two")

        self.assertEqual(len(first["replies"]), 1)
        self.assertEqual(second["replies"], [])
        self.assertEqual(self.queue.usage.summary("worker-key")["used"]["runs"], 1)
        row = self.queue.db.execute("SELECT agent FROM usage_runs").fetchone()
        self.assertEqual(row["agent"], "worker-key")

    def test_failed_alert_audit_rolls_back_dedup_so_alert_can_retry(self):
        agent = self.agent(plan=UsagePlan("daily", runs=2, warn_threshold=.5))
        self.queue.threads.configure({"agent-key": agent})
        self.queue.db.execute(
            "CREATE TRIGGER reject_usage_audit BEFORE INSERT ON audit_log "
            "WHEN NEW.event='usage_warn' BEGIN SELECT RAISE(ABORT, 'reject usage audit'); END"
        )
        thread = self.queue.threads.create("alert atomicity")

        result = self.queue.threads.post(thread["id"], "user", "@agent-key task: work")

        self.assertEqual(len(result["replies"]), 1)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM usage_alerts").fetchone()[0], 0)
        self.assertEqual(self.notifier.calls, [])
        self.queue.db.execute("DROP TRIGGER reject_usage_audit")
        run = self.queue.db.execute("SELECT * FROM usage_runs").fetchone()
        self.queue.usage.record(
            "agent-key", run_id=run["run_id"], started=run["started"],
            duration=run["duration"], ok=bool(run["ok"]), usage=None,
        )
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM usage_alerts").fetchone()[0], 1)
        self.assertEqual(len(self.notifier.calls), 1)


if __name__ == "__main__":
    unittest.main()
