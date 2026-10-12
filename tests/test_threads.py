from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hitl_queue import Queue
from hitl_queue.agents import AgentLimits, FakeAgent


class ThreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = Queue(self.root)
        limits = AgentLimits((self.root,), max_turns=2, timeout_seconds=1)
        self.agent = FakeAgent("Helper", limits, output="A useful reply")
        self.queue.threads.configure({"Helper": self.agent})

    def tearDown(self):
        self.queue.close()
        self.tmp.cleanup()

    def test_task_mention_runs_agent_and_fyi_does_not(self):
        thread = self.queue.threads.create("Example")
        result = self.queue.threads.post(
            thread["id"], "user", "@Helper fyi: context\n@Helper task: summarize"
        )
        self.assertEqual(len(self.agent.calls), 1)
        self.assertEqual(len(result["replies"]), 1)
        messages = self.queue.threads.messages(thread["id"])
        self.assertEqual([m["author_type"] for m in messages], ["user", "agent"])
        self.assertIn("Current task:\nsummarize", self.agent.calls[0][0])

    def test_agent_task_mention_wakes_another_agent(self):
        second = FakeAgent("Reviewer", self.agent.limits, output="reviewed")
        self.agent.result = self.agent.result.__class__(
            "@Reviewer task: check this", True, None, 0.0, 1
        )
        self.queue.threads.configure({"Helper": self.agent, "Reviewer": second})
        thread = self.queue.threads.create("Relay", max_agent_runs=2)
        result = self.queue.threads.post(thread["id"], "user", "@Helper task: draft")
        self.assertEqual(len(result["replies"]), 2)
        self.assertEqual(len(second.calls), 1)

    def test_thread_run_limit(self):
        thread = self.queue.threads.create("Bounded", max_agent_runs=1)
        self.queue.threads.post(thread["id"], "user", "@Helper task: one")
        with self.assertRaisesRegex(ValueError, "run limit"):
            self.queue.threads.post(thread["id"], "user", "@Helper task: two")
        self.assertEqual(len(self.agent.calls), 1)

    def test_pause_resume_and_kill_controls(self):
        thread = self.queue.threads.create("Controls")
        self.queue.threads.pause("Helper")
        self.assertEqual(self.queue.threads.post(
            thread["id"], "user", "@Helper task: paused"
        )["replies"], [])
        self.queue.threads.resume("Helper")
        self.queue.threads.post(thread["id"], "user", "@Helper task: active")
        self.queue.threads.kill()
        self.assertEqual(self.queue.threads.post(
            thread["id"], "user", "@Helper task: killed"
        )["replies"], [])
        self.queue.threads.resume()
        self.queue.threads.post(thread["id"], "user", "@Helper task: resumed")
        self.assertEqual(len(self.agent.calls), 2)

    def test_kill_stops_in_flight_fake_run(self):
        slow = FakeAgent("Slow", self.agent.limits, output="too late", delay_seconds=5)
        self.queue.threads.configure({"Slow": slow})
        thread = self.queue.threads.create("Kill")
        result = {}
        worker = threading.Thread(
            target=lambda: result.update(self.queue.threads.post(
                thread["id"], "user", "@Slow task: wait"
            ))
        )
        worker.start()
        deadline = time.monotonic() + 1
        while not slow.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.queue.threads.kill("Slow")
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["replies"], [])
        run = self.queue.db.execute("SELECT * FROM agent_runs").fetchone()
        self.assertEqual(run["status"], "killed")

    def test_audit_is_append_only_and_records_messages_runs_and_approval(self):
        thread = self.queue.threads.create("Audit")
        self.queue.threads.post(thread["id"], "user", "@Helper task: go")
        ticket = self.queue.enqueue(title="Approval")
        self.queue.actions.skip(ticket["id"])
        events = [row["event"] for row in self.queue.threads.audit_log()]
        self.assertEqual(events[:4], ["message", "agent_run_start", "agent_run_end", "message"])
        self.assertIn("approval", events)
        with self.assertRaisesRegex(Exception, "append-only"):
            self.queue.db.execute("DELETE FROM audit_log")

    def test_action_request_becomes_review_ticket_and_is_not_executed(self):
        self.agent.result = self.agent.result.__class__(
            'Please approve.\nACTION_REQUEST: {"action":"push","summary":"publish branch","target":"origin"}',
            True, None, 0.0, 1,
        )
        thread = self.queue.threads.create("Gate")
        self.queue.threads.post(thread["id"], "user", "@Helper task: ship")
        tickets = self.queue.list(status="review")
        self.assertEqual(len(tickets), 1)
        ticket = self.queue.get(tickets[0]["id"])
        self.assertEqual(ticket["payload"]["action_request"]["action"], "push")
        requested = [r for r in self.queue.threads.audit_log() if r["event"] == "approval_requested"]
        self.assertEqual(requested[0]["ticket_id"], ticket["id"])

    def test_tables_persist_in_existing_database(self):
        thread = self.queue.threads.create("Persistent")
        self.queue.close()
        self.queue = Queue(self.root)
        self.assertEqual(self.queue.threads.get(thread["id"])["title"], "Persistent")


if __name__ == "__main__":
    unittest.main()
