#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import json
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

    def test_create_ticket_uses_existing_format_and_persists(self):
        ticket = self.q.create_ticket({
            "title": "  Refresh guide  ", "company": " Example ", "url": " https://example.com ",
            "kind": "general", "reason": "First line\n  Second line\n", "owner": " agent-one ",
        })
        self.assertEqual(ticket["id"], "TKT-0001")
        self.assertEqual(ticket["status"], "new")
        self.assertEqual(ticket["title"], "Refresh guide")
        self.assertEqual(ticket["company"], "Example")
        self.assertEqual(ticket["url"], "https://example.com")
        self.assertEqual(ticket["owner"], "agent-one")
        self.assertEqual(ticket["kind"], "general")
        self.assertEqual(ticket["payload"], {
            "reason": "First line\n  Second line\n", "artifacts": {}, "notes": "",
        })
        self.assertEqual(self.q.history(ticket["id"])[0]["actor"], "human")
        self.assertNotIn("preferred_worker", ticket["payload"])
        self.q.close()
        self.assertEqual(self.q.get(ticket["id"]), ticket)
        next_ticket = self.q.enqueue(title="Existing enqueue still works")
        self.assertEqual(next_ticket["id"], "TKT-0002")
        self.assertIsNone(next_ticket["owner"])

    def test_edit_preserves_identity_workflow_and_unrelated_payload(self):
        ticket = self.q.enqueue(title="Original", company="Example")
        tid = ticket["id"]
        self.q.claim(tid, "cursor-draft")
        self.q.submit_draft(tid, actor="cursor-draft", resume="brief.md", research_summary="Research")
        self.q.set_preferred_worker(tid, "codex")
        before = self.q.get(tid)
        updated = self.q.edit_ticket(tid, {
            "title": " Updated ", "reason": "Details\nwith a second line", "owner": "claude",
            "company": "New project", "url": "https://example.com/new", "kind": "site",
        })
        for key in ("id", "created", "status"):
            self.assertEqual(updated[key], before[key])
        self.assertGreaterEqual(updated["updated"], before["updated"])
        self.assertEqual(updated["title"], "Updated")
        self.assertEqual(updated["owner"], "claude")
        self.assertEqual(updated["kind"], "site")
        self.assertEqual(updated["company"], "New project")
        self.assertEqual(updated["url"], "https://example.com/new")
        self.assertEqual(updated["payload"], {
            **before["payload"], "reason": "Details\nwith a second line",
        })
        event = self.q.history(tid)[-1]
        self.assertEqual((event["status"], event["actor"]), ("review", "human"))
        self.assertIn("Edited fields:", event["note"])
        self.q.close()
        self.assertEqual(self.q.get(tid), updated)

    def test_edit_partial_fields_and_clear_optional_fields(self):
        ticket = self.q.create_ticket({"title": "Keep", "reason": "Keep details", "owner": "agent"})
        edited = self.q.edit_ticket(ticket["id"], {"company": "Changed"})
        self.assertEqual(edited["title"], "Keep")
        self.assertEqual(edited["payload"], ticket["payload"])
        self.assertEqual(edited["owner"], "agent")
        edited = self.q.edit_ticket(ticket["id"], {"owner": "", "url": "", "reason": ""})
        self.assertIsNone(edited["owner"])
        self.assertIsNone(edited["url"])
        self.assertEqual(edited["payload"]["reason"], "")

    def test_ticket_validation_does_not_mutate_or_consume_ids(self):
        bad_fields = [None, [], "title", {}, {"title": " "}, {"title": 1},
                      {"title": "x" * 501}, {"reason": "x" * 100001},
                      {"owner": False}, {"company": None}, {"url": []},
                      {"kind": "unknown"}, {"title": "bad\x00title"},
                      {"id": "TKT-1234"}, {"status": "done"}, {"payload": {}},
                      {"created": 0}, {"preferred_worker": "codex"}]
        for fields in bad_fields:
            with self.subTest(fields=str(fields)[:80]), self.assertRaises(ValueError):
                self.q.create_ticket(fields)
        self.assertEqual(self.q.list(include_terminal=True), [])
        ticket = self.q.enqueue(title="Unchanged")
        self.assertEqual(ticket["id"], "TKT-0001")
        history = self.q.history(ticket["id"])
        for fields in bad_fields:
            with self.subTest(fields=str(fields)[:80]), self.assertRaises(ValueError):
                self.q.edit_ticket(ticket["id"], fields)
            self.assertEqual(self.q.get(ticket["id"]), ticket)
            self.assertEqual(self.q.history(ticket["id"]), history)
        with self.assertRaisesRegex(ValueError, "Unknown ticket"):
            self.q.edit_ticket("TKT-9999", {"title": "Missing"})
        with self.assertRaisesRegex(ValueError, "ticket_id"):
            self.q.edit_ticket([], {"title": "Missing"})

    def test_closed_tickets_and_frozen_handoffs_cannot_be_edited(self):
        for status in ("done", "skipped", "cancelled", "handoff"):
            ticket = self.q.enqueue(title=status)
            self.q.db.execute("UPDATE tickets SET status=? WHERE id=?", (status, ticket["id"]))
            before = self.q.get(ticket["id"])
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "Cannot edit"):
                self.q.edit_ticket(ticket["id"], {"title": "Changed"})
            self.assertEqual(self.q.get(ticket["id"]), before)
        ticket = self.q.enqueue(title="Blocked handoff")
        self.q.db.execute("UPDATE tickets SET status='blocked',payload=? WHERE id=?", (
            json.dumps({**ticket["payload"], "handoff": {"worker": "codex"}}), ticket["id"],
        ))
        with self.assertRaisesRegex(ValueError, "frozen handoff"):
            self.q.edit_ticket(ticket["id"], {"title": "Changed"})

    def test_edits_allowed_before_handoff(self):
        for status in ("new", "drafting", "review", "approved", "blocked"):
            ticket = self.q.enqueue(title=status)
            self.q.db.execute("UPDATE tickets SET status=? WHERE id=?", (status, ticket["id"]))
            with self.subTest(status=status):
                self.assertEqual(self.q.edit_ticket(ticket["id"], {"title": "Updated"})["status"], status)

    def test_ticket_writes_and_history_roll_back_together(self):
        with mock.patch.object(self.q, "_event", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.q.create_ticket({"title": "Rolled back"})
        self.assertEqual(self.q.list(include_terminal=True), [])
        ticket = self.q.create_ticket({"title": "Keep"})
        self.assertEqual(ticket["id"], "TKT-0001")
        history = self.q.history(ticket["id"])
        with mock.patch.object(self.q, "_event", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.q.edit_ticket(ticket["id"], {"title": "Rolled back", "reason": "New"})
        self.assertEqual(self.q.get(ticket["id"]), ticket)
        self.assertEqual(self.q.history(ticket["id"]), history)

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
