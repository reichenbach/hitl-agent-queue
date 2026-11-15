import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hitl_queue import setup
from hitl_queue.agents import load_agents
from hitl_queue.agents.config import tomllib
from hitl_queue.app import AppApi, OutboxQueue, WebviewApi
from hitl_queue.cli import main
from hitl_queue.notify import SlackNotifier, StdoutNotifier, get_notifier
from hitl_queue.queue import Queue
from hitl_queue.tray import TrayModel


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "state"
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_status_does_not_create_directory(self):
        status = setup.setup_status(self.state)
        self.assertTrue(status["needed"])
        self.assertFalse(status["settings_exist"])
        self.assertEqual(status["agents"], ["claude", "codex", "generic", "local"])
        self.assertEqual({n["name"] for n in status["notifiers"]},
                         {"stdout", "slack", "teams", "discord", "webhook", "cortland"})
        self.assertFalse(self.state.exists())

    def test_selected_agents_load_with_created_workspaces_and_plans(self):
        result = setup.setup_queue(self.state, ["claude", "local", "claude"])
        self.assertEqual(result["created"], ["agents.toml", "settings.json"])
        self.assertFalse(result["needed"])
        agents = load_agents(self.state / "agents.toml")
        self.assertEqual(list(agents), ["claude", "local"])
        for agent in agents.values():
            self.assertTrue(agent.work_dir.is_dir())
            self.assertEqual(agent.work_dir, self.root / "state-workspaces" / agent.name)
        document = tomllib.loads((self.state / "agents.toml").read_text())
        self.assertFalse(Path(document["agents"]["claude"]["work_dir"]).is_absolute())
        self.assertEqual(document["agents"]["claude"]["plan"]["tokens"], 1000000)
        queue = Queue(self.state)
        self.addCleanup(queue.close)
        queue.threads.configure_from(self.state / "agents.toml")
        self.assertIsInstance(queue.notifier, StdoutNotifier)

    def test_notifier_records_only_env_name_and_runtime_uses_it(self):
        with mock.patch.dict(os.environ, {"HITL_SLACK_WEBHOOK_URL": "secret-value"}):
            setup.setup_queue(self.state, ["codex"], "slack")
            settings = (self.state / "settings.json").read_text()
            self.assertEqual(json.loads(settings), {
                "notifier": "slack", "notifier_env": "HITL_SLACK_WEBHOOK_URL",
            })
            self.assertNotIn("secret-value", settings)
            self.assertNotIn("secret-value", (self.state / "agents.toml").read_text())
            queue = Queue(self.state)
            self.addCleanup(queue.close)
            self.assertIsInstance(queue.notifier, SlackNotifier)
            self.assertEqual(queue.notifier.url, "secret-value")
        with mock.patch.dict(os.environ, {"HITL_NOTIFY": "stdout"}):
            self.assertIsInstance(get_notifier(self.state), StdoutNotifier)

    def test_each_notifier_saves_only_its_environment_name(self):
        for entry in setup.setup_status(self.state)["notifiers"]:
            with self.subTest(notifier=entry["name"]):
                state = self.root / entry["name"]
                setup.setup_queue(state, ["codex"], entry["name"])
                settings = json.loads((state / "settings.json").read_text())
                self.assertEqual(settings, {
                    "notifier": entry["name"], "notifier_env": entry["env"],
                })

    def test_second_run_keeps_both_files_and_does_not_create_new_workspaces(self):
        setup.setup_queue(self.state, ["claude"], "stdout")
        before = {path.name: path.read_bytes() for path in self.state.iterdir()}
        result = setup.setup_queue(self.state, ["codex"], "slack")
        self.assertEqual(result["created"], [])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.state.iterdir()})
        self.assertFalse((self.root / "state-workspaces/codex").exists())

    def test_partial_setup_preserves_existing_file(self):
        for existing in ("agents.toml", "settings.json"):
            with self.subTest(existing=existing):
                state = self.root / existing
                state.mkdir()
                (state / existing).write_text("keep these exact bytes")
                result = setup.setup_queue(state, ["codex"])
                self.assertEqual((state / existing).read_text(), "keep these exact bytes")
                self.assertNotIn(existing, result["created"])

    def test_invalid_choices_do_not_write_anything(self):
        for agents, notifier in [([], "stdout"), (["missing"], "stdout"),
                                  (["codex"], "https://secret.example/hook")]:
            with self.subTest(agents=agents, notifier=notifier), self.assertRaises(ValueError):
                setup.setup_queue(self.state, agents, notifier)
            self.assertFalse(self.state.exists())

    def test_choices_come_from_example_including_additional_agents(self):
        example = self.root / "example.toml"
        example.write_text(
            "[agents.cursor]\ntype = 'fake'\nallowed_folders = ['workspaces/cursor']\n"
            "work_dir = 'workspaces/cursor'\nmax_turns = 1\ntimeout_seconds = 1\n"
        )
        with mock.patch.object(setup, "example_path", return_value=example):
            self.assertEqual(setup.setup_status(self.state)["agents"], ["cursor"])
            setup.setup_queue(self.state, ["cursor"])
        self.assertEqual(list(load_agents(self.state / "agents.toml")), ["cursor"])

    def test_example_is_packaged_and_matches_root_example(self):
        root_example = Path(__file__).resolve().parents[1] / "agents.example.toml"
        self.assertEqual(setup.example_path().read_bytes(), root_example.read_bytes())
        # Frozen __file__ points into the bundle, independent of cwd or user data.
        bundled = self.root / "bundle/hitl_queue"
        bundled.mkdir(parents=True)
        (bundled / "agents.example.toml").write_bytes(root_example.read_bytes())
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(setup, "__file__", str(bundled / "setup.py")):
            self.assertEqual(setup.setup_status(self.state)["agents"],
                             ["claude", "codex", "generic", "local"])

    def cli(self, *args):
        with redirect_stdout(io.StringIO()) as output:
            code = main(list(args))
        return code, json.loads(output.getvalue())

    def test_cli_setup_and_idempotent_rerun(self):
        code, result = self.cli("setup", "--state-dir", str(self.state),
                                "--agents", " codex,claude ", "--notifier", "teams")
        self.assertEqual(code, 0)
        self.assertEqual(result["created"], ["agents.toml", "settings.json"])
        code, result = self.cli("--state-dir", str(self.state), "setup")
        self.assertEqual(code, 0)
        self.assertEqual(result["created"], [])
        self.assertEqual(list(load_agents(self.state / "agents.toml")), ["codex", "claude"])

    def test_cli_check_is_read_only_before_and_after_setup(self):
        code, result = self.cli("setup", "--state-dir", str(self.state), "--force-check")
        self.assertEqual(code, 0)
        self.assertTrue(result["needed"])
        self.assertFalse(self.state.exists())
        setup.setup_queue(self.state, ["codex"])
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        code, result = self.cli("setup", "--state-dir", str(self.state), "--force-check")
        self.assertEqual(code, 0)
        self.assertFalse(result["needed"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()})

    def test_cli_invalid_choices_and_default_frozen_directory(self):
        for option, value in [("--agents", "unknown"), ("--notifier", "unknown")]:
            self.assertEqual(self.cli("setup", "--state-dir", str(self.state), option, value)[0], 1)
        self.assertFalse(self.state.exists())
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch("hitl_queue.paths.user_data_dir", return_value=self.root):
            self.assertEqual(self.cli("setup", "--agents", "codex")[0], 0)
        self.assertFalse(setup.needs_setup(self.state))

    def test_app_api_setup_configures_immediately_and_disappears_from_board(self):
        for api_type in (AppApi, WebviewApi):
            with self.subTest(api=api_type.__name__):
                queue = OutboxQueue(self.root / api_type.__name__)
                self.addCleanup(queue.close)
                api = api_type(queue)
                self.assertTrue(api.board()["setup"]["needed"])
                self.assertTrue(api.setup(["claude"], "slack")["ok"])
                self.assertFalse(api.board()["setup"]["needed"])
                self.assertEqual(api.usage()[0]["agent"], "claude")
                self.assertEqual(api.notifier()["configured"], "slack")
                self.assertFalse(api.notifier()["sends"])
                self.assertEqual(api.setup(["codex"])["created"], [])

    def test_app_rejects_invalid_input_and_demo_setup(self):
        queue = OutboxQueue(self.state)
        self.addCleanup(queue.close)
        api = AppApi(queue)
        for agents in ([], ["missing"], "codex", [None]):
            self.assertFalse(api.setup(agents)["ok"])
        self.assertTrue(setup.needs_setup(self.state))
        demo = AppApi(queue, demo=True)
        self.assertIsNone(demo.board()["setup"])
        self.assertFalse(demo.setup(["codex"])["ok"])

    def test_tray_finish_setup_opens_board_and_refresh_loads_configuration(self):
        queue = Queue(self.state)
        self.addCleanup(queue.close)
        launch = mock.Mock()
        model = TrayModel(queue, app_launcher=launch)
        next(item for item in model.items if item.text == "Finish setup...").action()
        launch.assert_called_once_with()
        setup.setup_queue(self.state, ["claude"], "slack")
        model.refresh()
        self.assertNotIn("Finish setup...", [item.text for item in model.items])
        self.assertIsNone(model.refresh_error)
        self.assertEqual(queue.usage.summary()[0]["agent"], "claude")
        self.assertIsInstance(queue.notifier, SlackNotifier)
        self.assertFalse(TrayModel(queue, demo=True).refresh_error)

    def test_demo_tray_never_offers_setup(self):
        queue = Queue(self.state)
        self.addCleanup(queue.close)
        model = TrayModel(queue, demo=True)
        self.assertNotIn("Finish setup...", [item.text for item in model.items])


if __name__ == "__main__":
    unittest.main()
