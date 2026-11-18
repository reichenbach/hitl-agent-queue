#!/usr/bin/env python3
from __future__ import annotations

import http.client
import inspect
import io
import json
import os
import re
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from hitl_queue import app
from hitl_queue.app import (
    BOARD_STATUSES,
    SIDE_LANE_STATUSES,
    AppApi,
    OutboxNotifier,
    OutboxQueue,
    WebviewApi,
    demo_config,
    make_server,
    seed_demo,
)
from hitl_queue.cli import main


class _DemoCase(unittest.TestCase):
    apply_opens: str | None = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = OutboxQueue(Path(self.tmp.name), config=demo_config(self.apply_opens))
        seed_demo(self.queue)
        self.api = AppApi(self.queue, demo=True)

    def tearDown(self):
        self.queue.close()
        self.tmp.cleanup()

    def first(self, status: str) -> str:
        return self.queue.list(status=status)[0]["id"]


class AppApiTests(_DemoCase):
    def test_seed_fills_every_lane_and_starts_with_empty_outbox(self):
        counts = self.queue.count_by_status()
        for status in BOARD_STATUSES + SIDE_LANE_STATUSES:
            self.assertGreater(counts[status], 0, status)
        self.assertEqual(self.queue.recent_notifications(), [])

    def test_board_has_status_columns_side_lane_and_gate(self):
        board = self.api.board()
        self.assertEqual([c["status"] for c in board["columns"]], BOARD_STATUSES)
        self.assertEqual([c["status"] for c in board["side_lane"]], SIDE_LANE_STATUSES)
        self.assertEqual(board["gate"]["apply_opens"], "2099-01-01")
        self.assertFalse(board["gate"]["open"])
        self.assertEqual(board["gate"]["mode"], "review_only")
        new_cards = board["columns"][0]["tickets"]
        self.assertIn("claude", [c["preferred_worker"] for c in new_cards])
        done_card = board["columns"][-1]["tickets"][0]
        self.assertFalse(done_card["can_approve"])
        self.assertFalse(done_card["can_skip"])
        json.dumps(board)

    def test_ticket_detail_has_files_and_history(self):
        tid = self.first("handoff")
        detail = self.api.ticket(tid)
        self.assertTrue(detail["ok"])
        files = detail["files"]
        self.assertEqual([f["label"] for f in files], ["brief", "checklist", "handoff packet"])
        self.assertTrue(all(f["exists"] for f in files))
        self.assertIn("Synthetic demo content", files[0]["preview"])
        self.assertEqual(json.loads(files[2]["preview"])["worker"], "codex")
        self.assertEqual(
            [e["status"] for e in detail["history"]],
            ["new", "drafting", "review", "approved", "handoff"],
        )

    def test_unknown_ticket(self):
        self.assertFalse(self.api.ticket("TKT-9999")["ok"])

    def test_files_outside_state_dir_are_not_read(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "private.txt"
            outside.write_text("private", encoding="utf-8")
            info = self.api._file_info("artifact", str(outside))
        self.assertFalse(info["exists"])
        self.assertIsNone(info["preview"])
        self.assertIn("not read", info["note"])
        self.assertNotIn("demo", info["note"])

    def test_symlinks_outside_state_dir_are_not_read(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "artifact.txt"
            outside.write_text("outside content", encoding="utf-8")
            link = self.queue.state / "linked-artifact.txt"
            link.symlink_to(outside)
            info = self.api._file_info("artifact", str(link))
        self.assertIsNone(info["preview"])
        self.assertIn("not read", info["note"])

    def test_approve_review_hands_off_in_review_only_mode(self):
        tid = self.first("review")
        out = self.api.approve(tid, "codex")
        self.assertEqual(
            {k: v for k, v in out.items() if k != "notification"},
            {"ok": True, "action": "handoff", "id": tid, "worker": "codex", "mode": "review_only"},
        )
        self.assertEqual(self.queue.get(tid)["status"], "handoff")
        outbox = self.api.notifier()["outbox"]
        self.assertTrue(outbox)
        self.assertEqual({m["channel"] for m in outbox}, {"outbox"})
        self.assertIn(f"OK {tid}", outbox[0]["text"])

    def test_skip_and_rejected_actions(self):
        tid = self.first("review")
        self.assertEqual({k: v for k, v in self.api.skip(tid).items() if k != "notification"}, {"ok": True, "action": "skip", "id": tid})
        self.assertEqual(self.queue.get(tid)["status"], "skipped")

        done = self.first("done")
        self.assertFalse(self.api.skip(done)["ok"])
        self.assertFalse(self.api.approve(done, "claude")["ok"])
        self.assertFalse(self.api.approve(self.first("review"), "gemini")["ok"])

    def test_mute_toggle_suppresses_but_records(self):
        self.assertTrue(self.api.set_mute(True)["muted"])
        self.api.skip(self.first("new"))
        outbox = self.api.notifier()["outbox"]
        self.assertTrue(outbox)
        self.assertEqual({m["channel"] for m in outbox}, {"muted"})
        self.assertFalse(self.api.set_mute(False)["muted"])

    def test_nothing_is_sent_even_if_env_selects_a_channel(self):
        with mock.patch.dict(os.environ, {"HITL_NOTIFY": "slack"}), mock.patch(
            "hitl_queue.notify._post_json", side_effect=AssertionError("network")
        ), tempfile.TemporaryDirectory() as tmp:
            queue = OutboxQueue(Path(tmp), config=demo_config())
            try:
                seed_demo(queue)
                api = AppApi(queue)
                self.assertIsInstance(queue.notifier, OutboxNotifier)
                self.assertEqual(api.notifier()["configured"], "slack")
                self.assertTrue(api.approve(queue.list(status="review")[0]["id"], "claude")["ok"])
            finally:
                queue.close()


class WebviewApiTests(_DemoCase):
    def test_each_call_releases_the_calling_threads_connection(self):
        api = WebviewApi(self.queue)
        main_connection = self.queue.db
        results = []

        def call():
            results.append(api.board())
            results.append(api.ticket(self.first("review")))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertTrue(results[1]["ok"])
        self.assertEqual(self.queue._all_connections, {main_connection})
        main_connection.execute("SELECT 1").fetchone()

    def test_exposes_same_public_methods_and_signatures(self):
        api = WebviewApi(self.queue)
        self.assertIsInstance(api, AppApi)
        self.assertEqual(
            list(inspect.signature(api.approve).parameters), ["ticket_id", "worker"]
        )
        self.assertNotIn("_release_thread", vars(WebviewApi))


class OpenGateTests(_DemoCase):
    apply_opens = "2000-01-01"

    def test_open_gate_hands_off_in_apply_mode(self):
        self.assertTrue(self.api.gate()["open"])
        out = self.api.approve(self.first("review"), "claude")
        self.assertEqual(out["mode"], "apply")

    def test_invalid_apply_opens(self):
        with self.assertRaises(ValueError):
            demo_config("not-a-date")


class HttpFallbackTests(_DemoCase):
    def setUp(self):
        super().setUp()
        self.httpd = make_server(self.api)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        page = self._get("/")
        self.assertIn('"transport": "http"', page)
        self.assertIn("<title>HITL Queue — demo data</title>", page)
        self.token = re.search(r'"token": "([^"]+)"', page).group(1)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def _get(self, path: str) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.read().decode()

    def _post(self, name, args=(), *, token=None, host=None, body=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Hitl-Token"] = token
        if host is not None:
            headers["Host"] = host
        data = body if body is not None else json.dumps({"args": list(args)}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/{name}", data=data, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_board_and_actions_require_token(self):
        self.assertEqual(self._post("board")[0], 401)
        self.assertEqual(self._post("board", token="wrong")[0], 401)
        code, board = self._post("board", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual([c["status"] for c in board["columns"]], BOARD_STATUSES)

        tid = self.first("review")
        code, out = self._post("approve", [tid, "claude"], token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(out["action"], "handoff")
        self.assertEqual(self.queue.get(tid)["status"], "handoff")

    def test_setup_uses_authenticated_bridge_and_returns_normal_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)  # keep the repo's tracked ./agents.toml out of the fallback
            queue = OutboxQueue(Path(tmp) / "state")
            try:
                with mock.patch.object(self.api, "_queue", queue), \
                        mock.patch.object(self.api, "_demo", False):
                    self.assertEqual(self._post("setup", [["claude"], "stdout"])[0], 401)
                    self.assertFalse((queue.state / "agents.toml").exists())
                    code, result = self._post("setup", [["claude"], "stdout"], token=self.token)
                    self.assertEqual(code, 200)
                    self.assertTrue(result["ok"])
                    code, board = self._post("board", token=self.token)
                    self.assertEqual(code, 200)
                    self.assertFalse(board["setup"]["needed"])
                    self.assertEqual(board["usage"][0]["agent"], "claude")
            finally:
                queue.close()
                os.chdir(old_cwd)

    def test_rejects_unknown_methods_bad_host_and_bad_args(self):
        self.assertEqual(self._post("_release_thread", token=self.token)[0], 404)
        self.assertEqual(
            self._post("board", token=self.token, host=f"evil.example:{self.port}")[0], 403
        )
        self.assertEqual(self._post("ticket", token=self.token, body=b'{"args": "x"}')[0], 400)
        self.assertEqual(self._post("ticket", token=self.token, body=b"not json")[0], 400)
        self.assertEqual(self._post("approve", ["TKT-0001"], token=self.token)[0], 400)

    def test_request_keeps_other_threads_connections_open(self):
        main_connection = self.queue.db
        self.assertEqual(self._post("board", token=self.token)[0], 200)
        main_connection.execute("SELECT 1").fetchone()
        self.assertEqual(self.queue._all_connections, {main_connection})

    def test_value_error_is_400_and_unexpected_error_is_500(self):
        with mock.patch.object(self.api, "board", side_effect=ValueError("bad input")):
            self.assertEqual(
                self._post("board", token=self.token), (400, {"ok": False, "error": "bad input"})
            )
        with mock.patch.object(self.api, "board", side_effect=RuntimeError("boom")), self.assertLogs(
            "hitl_queue.app", "ERROR"
        ) as logs:
            self.assertEqual(
                self._post("board", token=self.token),
                (500, {"ok": False, "error": "internal error"}),
            )
        self.assertIn("boom", "\n".join(logs.output))

    def test_responses_forbid_framing(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as resp:
            headers = resp.headers
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Content-Security-Policy"], "frame-ancestors 'none'")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/missing", timeout=5)
        self.assertEqual(ctx.exception.headers["X-Frame-Options"], "DENY")

    def _raw_post(self, content_length: str | None, body: bytes = b"") -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("POST", "/api/board", skip_accept_encoding=True)
            conn.putheader("X-Hitl-Token", self.token)
            if content_length is not None:
                conn.putheader("Content-Length", content_length)
            conn.endheaders(body or None)
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def test_rejects_bad_content_length(self):
        body = b'{"args": []}'
        self.assertEqual(self._raw_post(str(len(body)), body), 200)
        self.assertEqual(self._raw_post(None), 400)
        self.assertEqual(self._raw_post("abc"), 400)
        self.assertEqual(self._raw_post("-1"), 400)
        self.assertEqual(self._raw_post(str(app.MAX_BODY_BYTES + 1)), 413)


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


def _fake_webview(start=None):
    window = types.SimpleNamespace(events=types.SimpleNamespace(shown=_FakeEvent()))
    return types.SimpleNamespace(
        window=window,
        create_window=mock.Mock(return_value=window),
        start=mock.Mock(side_effect=start),
    )


class LauncherTests(unittest.TestCase):
    def _launch(self, webview_module, expect=0, **kwargs):
        kwargs.setdefault("demo", True)
        with mock.patch.dict(sys.modules, {"webview": webview_module}), mock.patch.object(
            app, "_run_browser", return_value=0
        ) as run_browser, redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(app.launch(**kwargs), expect)
        return run_browser, out.getvalue()

    def test_falls_back_to_browser_without_pywebview(self):
        run_browser, out = self._launch(None)
        run_browser.assert_called_once()
        self.assertIsInstance(run_browser.call_args.args[0], AppApi)
        self.assertIn("pywebview is not installed", out)

    def test_uses_pywebview_when_installed(self):
        fake = _fake_webview()
        run_browser, _ = self._launch(fake)
        run_browser.assert_not_called()
        kwargs = fake.create_window.call_args.kwargs
        self.assertEqual(fake.create_window.call_args.args[0], "HITL Queue — demo data")
        self.assertIn("HITL Queue — demo data", kwargs["html"])
        self.assertIsInstance(kwargs["js_api"], WebviewApi)
        self.assertIn('"transport": "pywebview"', kwargs["html"])
        fake.start.assert_called_once()

    def test_pywebview_start_failure_falls_back(self):
        fake = _fake_webview(start=RuntimeError("no GUI"))
        with self.assertLogs("hitl_queue.app", "ERROR") as logs:
            run_browser, out = self._launch(fake)
        run_browser.assert_called_once()
        self.assertNotIsInstance(run_browser.call_args.args[0], WebviewApi)
        self.assertIn("no GUI", out)
        self.assertIn("Traceback", "\n".join(logs.output))

    def test_pywebview_failure_after_window_opens_does_not_fall_back(self):
        fake = _fake_webview()

        def crash_after_shown():
            fake.window.events.shown.fire()
            raise RuntimeError("renderer crashed")

        fake.start.side_effect = crash_after_shown
        with self.assertLogs("hitl_queue.app", "ERROR") as logs:
            run_browser, out = self._launch(fake, expect=1)
        run_browser.assert_not_called()
        self.assertNotIn("opening in your browser", out)
        self.assertIn("renderer crashed", "\n".join(logs.output))

    def test_force_browser_skips_pywebview(self):
        fake = _fake_webview()
        run_browser, _ = self._launch(fake, force_browser=True)
        fake.create_window.assert_not_called()
        run_browser.assert_called_once()

    def test_cli_app_subcommand(self):
        with mock.patch.object(app, "launch", return_value=0) as launch:
            self.assertEqual(main(["app", "--demo", "--browser", "--apply-opens", "2026-01-01"]), 0)
        launch.assert_called_once_with(
            state_dir=None, demo=True, force_browser=True, port=0, apply_opens="2026-01-01"
        )

    def test_cli_app_state_dir_in_either_position(self):
        for argv, expected in [
            (["app"], None),
            (["--state-dir", "queue-state", "app"], "queue-state"),
            (["app", "--state-dir", "queue-state"], "queue-state"),
            (["--state-dir", "global", "app", "--state-dir", "local"], "local"),
        ]:
            with self.subTest(argv=argv), mock.patch.object(app, "launch", return_value=0) as launch:
                self.assertEqual(main(argv), 0)
                launch.assert_called_once_with(
                    state_dir=expected, demo=False, force_browser=False, port=0, apply_opens=None
                )

    def test_cli_apply_opens_requires_demo(self):
        with mock.patch.object(app, "launch") as launch, redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as raised:
                main(["app", "--apply-opens", "2026-01-01"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--apply-opens requires --demo", err.getvalue())
        launch.assert_not_called()


class RealAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.old_cwd)

    def test_real_mode_shows_existing_tickets_without_seeding(self):
        state = self.root / "queue-state"
        queue = OutboxQueue(state)
        try:
            ticket = queue.enqueue(title="Existing checklist")
        finally:
            queue.close()

        def check_board(api, **kwargs):
            cards = [t for col in api.board()["columns"] for t in col["tickets"]]
            self.assertEqual([t["id"] for t in cards], [ticket["id"]])
            self.assertEqual(cards[0]["title"], "Existing checklist")
            self.assertEqual(api._title, f"HITL Queue — {state}")
            return 0

        with mock.patch.object(app, "seed_demo") as seed, \
                mock.patch.object(app, "demo_config") as config, \
                mock.patch.object(app, "_run_browser", side_effect=check_board):
            self.assertEqual(app.launch(state_dir=state, force_browser=True), 0)
        seed.assert_not_called()
        config.assert_not_called()
        queue = OutboxQueue(state)
        try:
            self.assertEqual(len(queue.list(include_terminal=True)), 1)
        finally:
            queue.close()

    def test_state_local_agents_take_precedence_and_setup_stays_complete_on_reopen(self):
        from hitl_queue.setup import setup_queue

        state = self.root / "state"
        setup_queue(state, ["claude"], "teams")
        # A legacy file must not replace the newly saved state-local selection.
        (self.root / "agents.toml").write_text("invalid legacy TOML")

        def check_board(api, **kwargs):
            self.assertFalse(api.board()["setup"]["needed"])
            self.assertEqual(api.usage()[0]["agent"], "claude")
            self.assertEqual(api.notifier()["configured"], "teams")
            return 0

        with mock.patch.object(app, "_run_browser", side_effect=check_board), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app.launch(state_dir=state, force_browser=True), 0)

    def test_source_and_frozen_defaults_and_config(self):
        for frozen, explicit in [(False, False), (False, True), (True, False), (True, True)]:
            with self.subTest(frozen=frozen, explicit=explicit):
                data_dir = self.root / "user-data"
                data_dir.mkdir(exist_ok=True)
                config_dir = data_dir if frozen else self.root
                (config_dir / "work").mkdir(exist_ok=True)
                (config_dir / "agents.toml").write_text(
                    "[threads]\nmax_review_rounds = 5\n"
                    "[agents.worker]\ntype = 'fake'\nallowed_folders = ['work']\n"
                    "work_dir = 'work'\nmax_turns = 1\ntimeout_seconds = 1\n"
                    "[agents.worker.plan]\nwindow = 'daily'\nruns = 10\n",
                    encoding="utf-8",
                )
                state = self.root / "explicit-state" if explicit else config_dir / "state"

                def check_board(api, **kwargs):
                    self.assertEqual(api._queue.state, state)
                    self.assertEqual(api._queue.cfg["max_review_rounds"], 5)
                    self.assertEqual(api.usage()[0]["agent"], "worker")
                    self.assertFalse(any(c["tickets"] for c in api.board()["columns"]))
                    return 0

                with mock.patch.object(sys, "frozen", frozen, create=True), \
                        mock.patch.object(app, "user_data_dir", return_value=data_dir), \
                        mock.patch.object(app, "seed_demo") as seed, \
                        mock.patch.object(app, "_run_browser", side_effect=check_board):
                    self.assertEqual(app.launch(
                        state_dir=state if explicit else None, force_browser=True
                    ), 0)
                seed.assert_not_called()

    def test_demo_ignores_state_dir_and_cleans_up(self):
        real_state = self.root / "untouched-state"
        demo_states = []

        def check_board(api, **kwargs):
            demo_states.append(api._queue.state)
            self.assertNotEqual(api._queue.state, real_state)
            self.assertTrue(all(c["tickets"] for c in api.board()["columns"]))
            self.assertEqual(api.gate()["apply_opens"], "2000-01-01")
            self.assertEqual(api._title, "HITL Queue — demo data")
            return 0

        with mock.patch.object(app, "_run_browser", side_effect=check_board):
            self.assertEqual(app.launch(
                state_dir=real_state, demo=True, force_browser=True, apply_opens="2000-01-01"
            ), 0)
        self.assertFalse(real_state.exists())
        self.assertFalse(demo_states[0].exists())

    def test_real_window_and_http_page_show_state_path_safely(self):
        state = self.root / "queue <example> & data"
        fake = _fake_webview()
        with mock.patch.object(app, "_load_webview", return_value=fake):
            self.assertEqual(app.launch(state_dir=state), 0)
        self.assertEqual(fake.create_window.call_args.args[0], f"HITL Queue — {state}")
        self.assertIn("queue &lt;example&gt; &amp; data", fake.create_window.call_args.kwargs["html"])
        queue = OutboxQueue(state)
        self.addCleanup(queue.close)
        self.assertIn(
            "queue &lt;example&gt; &amp; data", app.render_page("http", title=AppApi(queue)._title)
        )


if __name__ == "__main__":
    unittest.main()
