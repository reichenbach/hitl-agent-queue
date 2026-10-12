import unittest
from unittest import mock
import sys

from hitl_queue.cli import main
from hitl_queue.tray import TrayModel, schedule_on_main_thread, usage_bar


class FakeActions:
    def __init__(self, queue):
        self.queue = queue
        self.calls = []

    def approve(self, ticket_id, worker):
        self.calls.append(("approve", ticket_id, worker))
        self.queue.tickets = []
        return {"ok": True}

    def skip(self, ticket_id):
        self.calls.append(("skip", ticket_id))
        self.queue.tickets = []
        return {"ok": True}


class FakeThreads:
    def running(self):
        return [{"agent": "Builder", "thread_id": "THR-0002", "status": "running"}]


class FakeUsage:
    def summary(self):
        return [
            {"agent": "Builder", "percent_used": {"runs": 52.0}, "warn": False,
             "blocked": False},
            {"agent": "Reviewer", "percent_used": {"tokens": 85.0}, "warn": True,
             "blocked": False},
            {"agent": "Writer", "percent_used": {"minutes": 105.0}, "warn": True,
             "blocked": True},
        ]


class FakeQueue:
    def __init__(self):
        self.cfg = {"default_apply_worker": "codex"}
        self.tickets = [{"id": "TKT-0001", "title": "Review draft", "status": "review"}]
        self.payload = {"preferred_worker": "claude"}
        self.actions = FakeActions(self)
        self.threads = FakeThreads()
        self.usage = FakeUsage()

    def list(self, *, status):
        return [item for item in self.tickets if item["status"] == status]

    def get(self, _ticket_id):
        return {"payload": self.payload}


class TrayModelTests(unittest.TestCase):
    def setUp(self):
        self.queue = FakeQueue()
        self.launched = []
        self.changed = []
        self.model = TrayModel(
            self.queue,
            app_launcher=lambda: self.launched.append(True),
            on_change=lambda: self.changed.append(True),
        )

    def section(self, name):
        return next(item for item in self.model.items if item.text == name)

    def test_builds_approvals_running_agents_and_usage_markers(self):
        approval = self.section("Pending approvals").children[0]
        self.assertEqual(approval.text, "TKT-0001 — Review draft")
        self.assertEqual([item.text for item in approval.children],
                         ["Approve → claude", "Skip"])
        self.assertIn("Builder — THR-0002 (running)",
                      self.section("Running agents").children[0].text)
        usage = [item.text for item in self.section("Usage").children]
        self.assertEqual(usage[0], "Builder [#####-----] 52%")
        self.assertTrue(usage[1].endswith("85% WARN"))
        self.assertTrue(usage[2].endswith("105% BLOCKED"))

    def test_approve_and_skip_items_call_actions_and_refresh(self):
        approval = self.section("Pending approvals").children[0]
        approval.children[0].action()
        self.assertEqual(self.queue.actions.calls, [("approve", "TKT-0001", "claude")])
        self.assertEqual(self.section("Pending approvals").children[0].text, "None")
        self.assertEqual(len(self.changed), 1)

        self.queue.tickets = [{"id": "TKT-0001", "title": "Review draft", "status": "review"}]
        self.model.refresh()
        self.section("Pending approvals").children[0].children[1].action()
        self.assertEqual(self.queue.actions.calls[-1], ("skip", "TKT-0001"))

    def test_open_app_uses_injected_launcher(self):
        next(item for item in self.model.items if item.text == "Open app").action()
        self.assertEqual(self.launched, [True])

    def test_ticket_title_is_single_line_bounded_and_strips_bidi_controls(self):
        self.queue.tickets[0]["title"] = "  Review\nthis\u0085\u202e " + "x" * 80
        self.model.refresh()
        label = self.section("Pending approvals").children[0].text
        self.assertNotIn("\n", label)
        self.assertNotIn("\u202e", label)
        self.assertNotIn("\u0085", label)
        self.assertTrue(label.endswith("…"))
        self.assertLessEqual(len(label.removeprefix("TKT-0001 — ")), 60)

    def test_windows_menu_title_escapes_ampersands(self):
        self.queue.tickets[0]["title"] = "Research & review"
        with mock.patch.object(sys, "platform", "win32"):
            self.model.refresh()
        self.assertIn("Research && review", self.section("Pending approvals").children[0].text)

    def test_action_failure_survives_successful_refresh(self):
        self.queue.actions.approve = mock.Mock(return_value={"ok": False, "error": "denied"})
        self.section("Pending approvals").children[0].children[0].action()
        self.model.refresh()
        self.assertEqual(self.model.action_error, "denied")
        self.assertIsNone(self.model.refresh_error)
        self.assertIn("Error: denied", [item.text for item in self.model.items])

    def test_main_thread_scheduler_is_direct_off_macos(self):
        called = []
        with mock.patch.object(sys, "platform", "linux"):
            schedule_on_main_thread(lambda: called.append(True))
        self.assertEqual(called, [True])

    def test_usage_bar_clamps_and_rounds(self):
        self.assertEqual(usage_bar(-1), "[----------]")
        self.assertEqual(usage_bar(52), "[#####-----]")
        self.assertEqual(usage_bar(500), "[##########]")

    def test_cli_tray_demo_dispatches_without_starting_ui(self):
        with mock.patch("hitl_queue.tray.launch", return_value=0) as launch:
            self.assertEqual(main(["--state-dir", "public-state", "tray", "--demo"]), 0)
        launch.assert_called_once_with(state_dir="public-state", demo=True)

    def test_cli_tray_accepts_state_dir_after_subcommand(self):
        with mock.patch("hitl_queue.tray.launch", return_value=0) as launch:
            self.assertEqual(main(["tray", "--state-dir", "public-state"]), 0)
        launch.assert_called_once_with(state_dir="public-state", demo=False)


if __name__ == "__main__":
    unittest.main()
