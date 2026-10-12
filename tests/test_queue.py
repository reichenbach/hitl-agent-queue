#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hitl_queue.queue import Queue
from hitl_queue.notify import StdoutNotifier, progress_text


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.q = Queue(
            Path(self.tmp.name),
            config={"apply_opens": "2099-01-01", "notify_progress": True},
            notifier=StdoutNotifier(),
        )

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()

    def test_prefer_then_auto_handoff(self):
        t = self.q.enqueue(title="Workflow Engineer", company="Acme", kind="job_draft")
        tid = t["id"]
        self.assertEqual(
            self.q.handle_reply(f"APPROVE {tid} CLAUDE")["action"],
            "prefer_worker",
        )
        self.q.claim(tid, "cursor-draft")
        resume = Path(self.tmp.name) / "r.txt"
        resume.write_text("resume")
        out = self.q.submit_draft(tid, actor="cursor-draft", resume=str(resume))
        self.assertEqual(out["status"], "handoff")
        self.assertEqual(out["payload"]["handoff"]["worker"], "claude")
        self.assertEqual(out["payload"]["handoff"]["mode"], "review_only")

    def test_skip(self):
        t = self.q.enqueue(title="Nope", company="Mill")
        self.assertTrue(self.q.handle_reply(f"SKIP {t['id']}")["ok"])
        self.assertEqual(self.q.get(t["id"])["status"], "skipped")

    def test_shared_actions_match_reply_behavior(self):
        t = self.q.enqueue(title="Direct action", company="Acme")
        out = self.q.actions.approve(t["id"], "CODEX")
        self.assertEqual(out["action"], "prefer_worker")
        self.assertEqual(self.q.get(t["id"])["payload"]["preferred_worker"], "codex")

        other = self.q.enqueue(title="Direct skip", company="Acme")
        out = self.q.actions.skip(other["id"])
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["id"], other["id"])
        self.assertTrue(out["notification"]["ok"])

    def test_action_responses_include_failed_notification(self):
        notifier = mock.Mock()
        notifier.notify.return_value = {
            "ok": False,
            "channel": "test",
            "error": "offline",
        }
        ticket = self.q.enqueue(title="Cannot notify", company="Acme")
        other = self.q.enqueue(title="Cannot notify either", company="Acme")
        self.q.notifier = notifier

        with self.assertLogs("hitl_queue.notify", level="WARNING"):
            skipped = self.q.actions.skip(ticket["id"])
            approved = self.q.actions.approve(other["id"], "CODEX")

        expected = {"ok": False, "channel": "test", "error": "offline"}
        self.assertEqual(skipped["notification"], expected)
        self.assertEqual(approved["notification"], expected)

    def test_history_and_status_counts(self):
        first = self.q.enqueue(title="First")
        second = self.q.enqueue(title="Second")
        self.q.claim(first["id"], "cursor-draft")
        self.q.actions.skip(second["id"])

        history = self.q.history(first["id"])
        self.assertEqual([event["status"] for event in history], ["new", "drafting"])
        self.assertEqual(self.q.status_history(first["id"]), history)
        counts = self.q.count_by_status()
        self.assertEqual(counts["drafting"], 1)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self.q.status_counts(), counts)

    def test_one_queue_is_safe_across_threads(self):
        errors = []
        ids = []
        lock = threading.Lock()

        def enqueue(index):
            try:
                ticket = self.q.enqueue(title=f"Thread {index}")
                with lock:
                    ids.append(ticket["id"])
            except Exception as exc:  # pragma: no cover - asserted below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=enqueue, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 8)

    def test_close_closes_connections_created_by_all_threads(self):
        connections = []
        lock = threading.Lock()

        def connect():
            connection = self.q.db
            connection.execute("SELECT 1").fetchone()
            with lock:
                connections.append(connection)

        threads = [threading.Thread(target=connect) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(connections), 3)
        self.q.close()
        for connection in connections:
            with self.assertRaisesRegex(Exception, "closed database"):
                connection.execute("SELECT 1")

    def test_close_thread_closes_only_the_calling_threads_connection(self):
        main_connection = self.q.db
        worker = {}

        def connect_and_release():
            worker["connection"] = self.q.db
            self.q.close_thread()
            self.q.close_thread()

        thread = threading.Thread(target=connect_and_release)
        thread.start()
        thread.join(timeout=5)

        with self.assertRaisesRegex(Exception, "closed database"):
            worker["connection"].execute("SELECT 1")
        main_connection.execute("SELECT 1").fetchone()
        self.assertEqual(self.q._all_connections, {main_connection})

        self.q.close_thread()
        with self.assertRaisesRegex(Exception, "closed database"):
            main_connection.execute("SELECT 1")
        self.assertIsNot(self.q.db, main_connection)
        self.q.db.execute("SELECT 1").fetchone()

    def test_progress_text(self):
        text = progress_text(
            {
                "id": "TKT-0001",
                "company": "Acme",
                "title": "Engineer",
                "payload": {"preferred_worker": "codex"},
            },
            "drafting",
            detail="claimed",
        )
        self.assertIn("TKT-0001 drafting", text)
        self.assertIn("Worker pick: codex", text)


if __name__ == "__main__":
    unittest.main()
