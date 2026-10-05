"""Minimal reply ingress for Slack slash commands / generic webhooks.

Outbound chat apps (Slack, Teams, Discord) push progress via webhooks.
Inbound approval still uses the same grammar:

  APPROVE TKT-0001 CLAUDE
  SKIP TKT-0001

Wire a Slack slash command or an automation to POST here.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .queue import Queue


def _extract_text(handler: BaseHTTPRequestHandler, raw: bytes) -> str:
    ctype = (handler.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return ""
        # Slack Events / interactive / generic
        if isinstance(obj.get("text"), str):
            return obj["text"]
        if isinstance(obj.get("event"), dict) and isinstance(obj["event"].get("text"), str):
            return obj["event"]["text"]
        if isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("text"), str):
            return obj["message"]["text"]
        return ""
    # Slack slash commands are usually application/x-www-form-urlencoded
    form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    if "text" in form and form["text"]:
        return form["text"][0]
    return raw.decode("utf-8", "replace").strip()


def make_handler(state_dir: Path, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            # Keep default access log short.
            print("[reply-server]", fmt % args)

        def _unauthorized(self) -> None:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"unauthorized"}')

        def _check_auth(self) -> bool:
            if not token:
                return True
            got = self.headers.get("X-Hitl-Token") or ""
            auth = self.headers.get("Authorization") or ""
            if got == token:
                return True
            if auth.startswith("Bearer ") and auth[7:] == token:
                return True
            # Slack signing is better; token header is the portable demo.
            form_token = ""
            return False

        def do_GET(self) -> None:
            if self.path.rstrip("/") in {"", "/health"}:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"service":"hitl-reply-server"}')
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if token:
                # Allow Slack slash `token` form field as alternate for demos.
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                ctype = (self.headers.get("Content-Type") or "").lower()
                ok = False
                if self.headers.get("X-Hitl-Token") == token:
                    ok = True
                auth = self.headers.get("Authorization") or ""
                if auth.startswith("Bearer ") and auth[7:] == token:
                    ok = True
                if "application/x-www-form-urlencoded" in ctype:
                    form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                    if form.get("token", [""])[0] == token:
                        ok = True
                if not ok:
                    self._unauthorized()
                    return
            else:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)

            text = _extract_text(self, raw)
            q = Queue(state_dir)
            try:
                result = q.handle_reply(text)
            finally:
                q.close()

            # Slack slash commands expect a text response body.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if result.get("ok"):
                msg = result.get("action", "ok")
                payload = {
                    **result,
                    "response_type": "ephemeral",
                    "text": f"hitl-queue: {msg} {result.get('id', '')}".strip(),
                }
            else:
                payload = {
                    **result,
                    "response_type": "ephemeral",
                    "text": f"hitl-queue: {result.get('error', 'failed')}",
                }
            self.wfile.write(json.dumps(payload).encode("utf-8"))

    return Handler


def serve(state_dir: str = "state", host: str = "127.0.0.1", port: int = 8787) -> None:
    token = os.environ.get("HITL_REPLY_TOKEN") or None
    handler = make_handler(Path(state_dir), token)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"hitl reply server on http://{host}:{port}  (POST text / Slack slash)")
    if token:
        print("auth: X-Hitl-Token or Bearer or Slack form token")
    httpd.serve_forever()
