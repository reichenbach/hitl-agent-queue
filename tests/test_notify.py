#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from hitl_queue.notify import (
    DiscordNotifier,
    GenericWebhookNotifier,
    MultiNotifier,
    SlackNotifier,
    StdoutNotifier,
    TeamsNotifier,
    build_notifier,
    dispatch,
    get_notifier,
)
from hitl_queue.queue import Queue
from hitl_queue.reply_server import make_handler


class _CaptureHandler(BaseHTTPRequestHandler):
    last_body: bytes = b""
    last_path: str = ""

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        type(self).last_body = self.rfile.read(length)
        type(self).last_path = self.path
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


class NotifyTests(unittest.TestCase):
    def test_build_unknown(self):
        with self.assertRaises(ValueError):
            build_notifier("pagerduty")

    def test_get_notifier_multi(self):
        with mock.patch.dict(
            os.environ,
            {"HITL_NOTIFY": "multi", "HITL_NOTIFY_CHANNELS": "stdout,slack"},
            clear=False,
        ):
            n = get_notifier()
        self.assertIsInstance(n, MultiNotifier)
        self.assertEqual(len(n.notifiers), 2)

    def test_slack_posts_json(self):
        httpd = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.handle_request, daemon=True)
        thread.start()
        n = SlackNotifier(webhook_url=f"http://127.0.0.1:{port}/hook")
        out = n.notify("hello review", source="test")
        thread.join(timeout=2)
        httpd.server_close()
        self.assertTrue(out["ok"])
        payload = json.loads(_CaptureHandler.last_body.decode())
        self.assertIn("hello review", payload["text"])
        self.assertIn("[test]", payload["text"])

    def test_teams_message_card(self):
        httpd = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.handle_request, daemon=True)
        thread.start()
        n = TeamsNotifier(webhook_url=f"http://127.0.0.1:{port}/teams")
        out = n.notify("line1\nline2", source="gate")
        thread.join(timeout=2)
        httpd.server_close()
        self.assertTrue(out["ok"])
        payload = json.loads(_CaptureHandler.last_body.decode())
        self.assertEqual(payload["@type"], "MessageCard")
        self.assertIn("line1", payload["text"])

    def test_discord_and_webhook(self):
        httpd = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/w"
        thread = threading.Thread(target=httpd.handle_request, daemon=True)
        thread.start()
        self.assertTrue(DiscordNotifier(webhook_url=url).notify("d")["ok"])
        thread.join(timeout=2)

        httpd2 = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        port2 = httpd2.server_address[1]
        thread2 = threading.Thread(target=httpd2.handle_request, daemon=True)
        thread2.start()
        out = GenericWebhookNotifier(webhook_url=f"http://127.0.0.1:{port2}/g").notify(
            "z", source="s"
        )
        thread2.join(timeout=2)
        httpd.server_close()
        httpd2.server_close()
        self.assertTrue(out["ok"])
        payload = json.loads(_CaptureHandler.last_body.decode())
        self.assertEqual(payload, {"text": "z", "source": "s"})

    def test_multi_any_ok(self):
        class Fail:
            def notify(self, text, *, source="hitl-queue"):
                return {"ok": False}

        class Ok:
            def notify(self, text, *, source="hitl-queue"):
                return {"ok": True, "channel": "ok"}

        out = MultiNotifier([Fail(), Ok()]).notify("x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["channel"], "multi")

    def test_missing_webhook_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(SlackNotifier().notify("x")["ok"])
            self.assertFalse(TeamsNotifier().notify("x")["ok"])

    def test_dispatch_can_be_muted_and_reports_outcome(self):
        class Unexpected:
            def notify(self, text, *, source="hitl-queue"):
                raise AssertionError("muted dispatch must not notify")

        self.assertEqual(
            dispatch(Unexpected(), "quiet", mute=True),
            {"channel": "muted", "ok": True, "error": None},
        )
        out = dispatch(SlackNotifier(webhook_url=""), "fails", mute=False)
        self.assertEqual(out["channel"], "slack")
        self.assertFalse(out["ok"])
        self.assertIn("error", out)

    def test_queue_honors_mute_environment_when_config_is_unset(self):
        class Unexpected:
            def notify(self, text, *, source="hitl-queue"):
                raise AssertionError("HITL_NOTIFY_MUTE must prevent notification")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"HITL_NOTIFY_MUTE": "true"}, clear=False
        ):
            queue = Queue(Path(tmp), config={"notify_progress": False}, notifier=Unexpected())
            try:
                self.assertEqual(
                    queue._notify("quiet", source="test"),
                    {"channel": "muted", "ok": True, "error": None},
                )
            finally:
                queue.close()

    def test_dispatch_logs_failed_result_and_exception(self):
        class Failed:
            def notify(self, text, *, source="hitl-queue"):
                return {"ok": False, "channel": "test", "error": "delivery failed"}

        class Broken:
            def notify(self, text, *, source="hitl-queue"):
                raise RuntimeError("notifier crashed")

        with self.assertLogs("hitl_queue.notify", level="WARNING") as logs:
            failed = dispatch(Failed(), "one")
            broken = dispatch(Broken(), "two")

        self.assertFalse(failed["ok"])
        self.assertFalse(broken["ok"])
        self.assertIn("delivery failed", logs.output[0])
        self.assertIn("notifier crashed", logs.output[1])


class ReplyServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.q = Queue(
            self.state,
            config={"apply_opens": "2099-01-01", "notify_progress": False},
            notifier=StdoutNotifier(),
        )
        self.ticket = self.q.enqueue(title="Eng", company="Acme")
        self.q.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _serve_once(self, token: str | None):
        handler = make_handler(self.state, token)
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.handle_request, daemon=True)
        thread.start()
        return httpd, port, thread

    def test_json_approve_prefer(self):
        import urllib.request

        httpd, port, thread = self._serve_once(token="secret")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=json.dumps(
                {"text": f"APPROVE {self.ticket['id']} CLAUDE"}
            ).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Hitl-Token": "secret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        thread.join(timeout=2)
        httpd.server_close()
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "prefer_worker")
        self.assertIn("prefer_worker", body["text"])

    def test_unauthorized(self):
        import urllib.error
        import urllib.request

        httpd, port, thread = self._serve_once(token="secret")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            data=b'{"text":"SKIP X"}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)
        thread.join(timeout=2)
        httpd.server_close()

    def test_health(self):
        import urllib.request

        httpd, port, thread = self._serve_once(token=None)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            body = json.loads(resp.read().decode())
        thread.join(timeout=2)
        httpd.server_close()
        self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main()
