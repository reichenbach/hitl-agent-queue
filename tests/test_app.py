#!/usr/bin/env python3
from __future__ import annotations

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
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hitl_queue import app
from hitl_queue.app import (
    BOARD_STATUSES,
    SIDE_LANE_STATUSES,
    AppApi,
    OutboxNotifier,
    OutboxQueue,
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
        self.api = AppApi(self.queue)

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
        self.assertEqual([f["label"] for f in files], ["cover_letter", "resume", "handoff packet"])
        self.assertTrue(all(f["exists"] for f in files))
        self.assertIn("Synthetic resume", files[1]["preview"])
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
            info = self.api._file_info("resume", str(outside))
        self.assertFalse(info["exists"])
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

    def test_rejects_unknown_methods_bad_host_and_bad_args(self):
        self.assertEqual(self._post("_release_thread", token=self.token)[0], 404)
        self.assertEqual(
            self._post("board", token=self.token, host=f"evil.example:{self.port}")[0], 403
        )
        self.assertEqual(self._post("ticket", token=self.token, body=b'{"args": "x"}')[0], 400)
        self.assertEqual(self._post("ticket", token=self.token, body=b"not json")[0], 400)
        self.assertEqual(self._post("approve", ["TKT-0001"], token=self.token)[0], 400)


class LauncherTests(unittest.TestCase):
    def _launch(self, webview_module, **kwargs):
        with mock.patch.dict(sys.modules, {"webview": webview_module}), mock.patch.object(
            app, "_run_browser", return_value=0
        ) as run_browser, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.launch(**kwargs), 0)
        return run_browser, out.getvalue()

    def test_falls_back_to_browser_without_pywebview(self):
        run_browser, out = self._launch(None)
        run_browser.assert_called_once()
        self.assertIsInstance(run_browser.call_args.args[0], AppApi)
        self.assertIn("pywebview is not installed", out)

    def test_uses_pywebview_when_installed(self):
        fake = types.SimpleNamespace(create_window=mock.Mock(), start=mock.Mock())
        run_browser, _ = self._launch(fake)
        run_browser.assert_not_called()
        kwargs = fake.create_window.call_args.kwargs
        self.assertIsInstance(kwargs["js_api"], AppApi)
        self.assertIn('"transport": "pywebview"', kwargs["html"])
        fake.start.assert_called_once()

    def test_pywebview_start_failure_falls_back(self):
        fake = types.SimpleNamespace(
            create_window=mock.Mock(), start=mock.Mock(side_effect=RuntimeError("no GUI"))
        )
        run_browser, out = self._launch(fake)
        run_browser.assert_called_once()
        self.assertIn("no GUI", out)

    def test_force_browser_skips_pywebview(self):
        fake = types.SimpleNamespace(create_window=mock.Mock(), start=mock.Mock())
        run_browser, _ = self._launch(fake, force_browser=True)
        fake.create_window.assert_not_called()
        run_browser.assert_called_once()

    def test_cli_app_subcommand(self):
        with mock.patch.object(app, "launch", return_value=0) as launch:
            self.assertEqual(main(["app", "--browser", "--apply-opens", "2026-01-01"]), 0)
        launch.assert_called_once_with(force_browser=True, port=0, apply_opens="2026-01-01")


if __name__ == "__main__":
    unittest.main()
