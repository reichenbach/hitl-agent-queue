#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
