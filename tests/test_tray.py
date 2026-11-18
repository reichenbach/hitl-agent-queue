import unittest
from unittest import mock
import sys
import runpy
from pathlib import Path

import hitl_queue.tray as tray
from hitl_queue.cli import main
from hitl_queue.tray import (
    TrayApplication, TrayModel, schedule_on_main_thread, usage_bar, user_data_dir,
)


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
        self.state = Path("queue-state").resolve()
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

    def test_open_app_command_uses_same_state_dir_and_demo_mode(self):
        for frozen in (False, True):
            for demo in (False, True):
                with self.subTest(frozen=frozen, demo=demo), \
                        mock.patch.object(sys, "frozen", frozen, create=True), \
                        mock.patch.object(sys, "executable", "HITL Queue" if frozen else "python"), \
                        mock.patch.object(tray.subprocess, "Popen") as popen:
                    model = TrayModel(self.queue, demo=demo)
                    result = next(item for item in model.items if item.text == "Open app").action()
                    command = [sys.executable, "app"] if frozen else [
                        sys.executable, "-m", "hitl_queue", "app"
                    ]
                    command.extend(("--state-dir", str(self.queue.state)))
                    if demo:
                        command.append("--demo")
                    popen.assert_called_once_with(command)
                    self.assertIs(result, popen.return_value)

    def test_tray_launch_passes_demo_mode_to_model(self):
        for demo in (False, True):
            with self.subTest(demo=demo), \
                    mock.patch.object(tray, "Queue", return_value=self.queue), \
                    mock.patch.object(tray, "TrayModel") as model, \
                    mock.patch.object(tray, "TrayApplication") as application, \
                    mock.patch.object(self.queue.threads, "configure_from", create=True), \
                    mock.patch("hitl_queue.app.seed_demo"), \
                    mock.patch("hitl_queue.app.OutboxQueue", return_value=self.queue):
                self.queue.close = mock.Mock()
                application.return_value.poll_thread = None
                self.assertEqual(tray.launch(state_dir="queue-state", demo=demo), 0)
                model.assert_called_once_with(self.queue, demo=demo)

    def test_frozen_open_app_command_routes_through_packaged_launcher(self):
        launcher = Path(__file__).resolve().parents[1] / "packaging/hitl_launcher.py"
        for demo in (False, True):
            with self.subTest(demo=demo), \
                    mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", "HITL Queue"), \
                    mock.patch.object(tray.subprocess, "Popen") as popen:
                tray.launch_desktop_app(state_dir=self.queue.state, demo=demo)
                command = popen.call_args.args[0]
                with mock.patch.object(sys, "argv", command), \
                        mock.patch("hitl_queue.app.launch", return_value=0) as launch:
                    with self.assertRaises(SystemExit) as raised:
                        runpy.run_path(str(launcher), run_name="__main__")
                self.assertEqual(raised.exception.code, 0)
                launch.assert_called_once_with(
                    state_dir=str(self.queue.state), demo=demo,
                    force_browser=False, port=0, apply_opens=None,
                )

    def test_packaged_launcher_defaults_to_tray_and_forwards_options(self):
        launcher = Path(__file__).resolve().parents[1] / "packaging/hitl_launcher.py"
        for arguments, state_dir, demo in (
            ([], None, False),
            (["--state-dir", "public state"], "public state", False),
            (["--demo"], None, True),
        ):
            with self.subTest(arguments=arguments), \
                    mock.patch.object(sys, "argv", ["HITL Queue", *arguments]), \
                    mock.patch("hitl_queue.tray.launch", return_value=0) as launch:
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(launcher), run_name="__main__")
            self.assertEqual(raised.exception.code, 0)
            launch.assert_called_once_with(state_dir=state_dir, demo=demo)

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

    def test_error_rows_are_sanitized(self):
        self.queue.actions.approve = mock.Mock(
            return_value={"ok": False, "error": " denied\nAPPROVE TKT-9999\u202e "}
        )
        self.section("Pending approvals").children[0].children[0].action()
        error = next(item.text for item in self.model.items if item.text.startswith("Error:"))
        self.assertEqual(error, "Error: denied APPROVE_ TKT-9999")

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

    def test_empty_platform_data_environment_uses_default(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict("os.environ", {"XDG_DATA_HOME": ""}), \
                mock.patch("pathlib.Path.home", return_value=tray.Path("/home/tester")):
            self.assertEqual(user_data_dir(), tray.Path("/home/tester/.local/share/hitl-queue"))
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict("os.environ", {"APPDATA": ""}), \
                mock.patch("pathlib.Path.home", return_value=tray.Path("C:/Users/tester")):
            self.assertEqual(
                user_data_dir(), tray.Path("C:/Users/tester/AppData/Roaming/HITL Queue")
            )

    def test_quit_logs_when_poll_thread_does_not_stop(self):
        application = TrayApplication.__new__(TrayApplication)
        application.stopped = mock.Mock()
        application.poll_thread = mock.Mock()
        application.poll_thread.is_alive.return_value = True
        application.join_timeout = 0.01
        application.icon = mock.Mock()

        with self.assertLogs("hitl_queue.tray", level="WARNING") as logs:
            application.quit()

        application.poll_thread.join.assert_called_once_with(0.01)
        application.icon.stop.assert_called_once_with()
        self.assertIn("deferring queue cleanup", logs.output[0])

    def test_launch_leaves_queue_open_while_poll_thread_is_alive(self):
        queue = mock.Mock()
        application = mock.Mock()
        application.poll_thread.is_alive.return_value = True
        with mock.patch.object(tray, "Queue", return_value=queue), \
                mock.patch.object(tray, "TrayModel"), \
                mock.patch.object(tray, "TrayApplication", return_value=application), \
                self.assertLogs("hitl_queue.tray", level="WARNING"):
            self.assertEqual(tray.launch(state_dir="public-state"), 0)
        queue.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
