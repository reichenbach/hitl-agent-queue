"""Desktop review board for the HITL queue (synthetic demo data only).

``python3 -m hitl_queue app`` opens the board in a pywebview window when the
optional ``pywebview`` dependency is installed (``pip install -e ".[app]"``),
otherwise in the default browser via a localhost-only HTTP server.

The app always runs against a throwaway state directory seeded with demo
tickets. Buttons call ``Queue.actions``; notifications land in an in-memory
outbox shown in the UI and are never sent anywhere.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from collections import deque
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .queue import DEFAULT_CONFIG, TRANSITIONS, Queue, apply_gate_open

BOARD_STATUSES = ["new", "drafting", "review", "approved", "handoff", "done"]
SIDE_LANE_STATUSES = ["blocked", "skipped", "cancelled"]
APPROVABLE = {"new", "drafting", "review", "approved"}
API_METHODS = ("board", "ticket", "approve", "skip", "set_mute")
PREVIEW_BYTES = 4000
PAGE = Path(__file__).with_name("app.html")


class OutboxNotifier:
    """Accepts every message without delivering it anywhere."""

    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        return {"ok": True, "channel": "outbox"}


class OutboxQueue(Queue):
    """Queue whose notifications are recorded locally for the notifier panel."""

    def __init__(self, state_dir: Path, *, config: dict | None = None, outbox_size: int = 50):
        super().__init__(state_dir, config=config, notifier=OutboxNotifier())
        self.outbox: deque[dict] = deque(maxlen=outbox_size)
        self._outbox_lock = threading.Lock()

    def _notify(self, text: str, *, source: str) -> dict:
        result = super()._notify(text, source=source)
        with self._outbox_lock:
            self.outbox.appendleft(
                {
                    "text": text,
                    "source": source,
                    "channel": result.get("channel"),
                    "ok": bool(result.get("ok")),
                    "at": time.time(),
                }
            )
        return result

    def recent_notifications(self) -> list[dict]:
        with self._outbox_lock:
            return list(self.outbox)

    def clear_notifications(self) -> None:
        with self._outbox_lock:
            self.outbox.clear()


def demo_config(apply_opens: str | None = None) -> dict:
    config = {**DEFAULT_CONFIG, "progress_events": list(DEFAULT_CONFIG["progress_events"])}
    if apply_opens:
        date.fromisoformat(apply_opens)
        config["apply_opens"] = apply_opens
    return config


def _demo_file(queue: Queue, name: str, text: str) -> str:
    path = queue.state / "demo-files" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _force_status(queue: Queue, tid: str, status: str, note: str) -> None:
    queue.transaction(lambda: queue._set(tid, status, "demo", note))


def _demo_ticket(queue: Queue, n: int, title: str, company: str, *, kind: str = "job_draft") -> str:
    return queue.enqueue(
        title=title,
        company=company,
        url=f"https://example.com/jobs/{n}",
        kind=kind,
        reason="Synthetic demo ticket",
        actor="demo",
    )["id"]


def _drafted(queue: Queue, n: int, title: str, company: str) -> str:
    tid = _demo_ticket(queue, n, title, company)
    queue.claim(tid, "cursor-draft")
    slug = tid.lower()
    queue.submit_draft(
        tid,
        actor="cursor-draft",
        resume=_demo_file(
            queue,
            f"{slug}-resume.md",
            f"# Synthetic resume for {title}\n\n- Built durable agent work queues\n"
            "- Shipped human-in-the-loop approval flows\n",
        ),
        cover_letter=_demo_file(
            queue,
            f"{slug}-cover-letter.md",
            f"Hello {company} team,\n\nThis is a synthetic cover letter for the "
            f"{title} demo ticket.\n",
        ),
        research_summary=f"{company} (synthetic): remote-friendly, workflow automation focus.",
    )
    return tid


def seed_demo(queue: OutboxQueue) -> None:
    """Populate every board column and the side lane with synthetic tickets."""
    mode = "apply" if apply_gate_open(queue.cfg) else "review_only"

    _demo_ticket(queue, 1, "Workflow Automation Engineer", "Example Logistics")
    site = _demo_ticket(queue, 2, "Portfolio landing page refresh", "Example Studio", kind="site")
    queue.actions.approve(site, "claude")

    drafting = _demo_ticket(queue, 3, "Agent Platform Engineer", "Example Bank")
    queue.claim(drafting, "cursor-draft")

    _drafted(queue, 4, "AI Workflow Engineer", "Example Health")
    _drafted(queue, 5, "Internal Tools Engineer", "Example Retail")

    approved = _drafted(queue, 6, "Solutions Engineer, Automation", "Example Freight")
    queue.approve(approved, actor="human", note="demo approval")

    handoff = _drafted(queue, 7, "Developer Experience Engineer", "Example Cloud")
    queue.approve(handoff, actor="human", note="demo approval")
    queue.handoff(handoff, to="codex", actor="human", mode=mode)

    done = _drafted(queue, 8, "Support Automation Lead", "Example Telecom")
    queue.approve(done, actor="human", note="demo approval")
    queue.handoff(done, to="claude", actor="human", mode=mode)
    queue.done(done, actor="claude", note=f"{mode} complete (synthetic)")

    blocked = _demo_ticket(queue, 9, "Platform Reliability Engineer", "Example Energy")
    queue.claim(blocked, "cursor-draft")
    _force_status(queue, blocked, "blocked", "waiting on synthetic portfolio link")

    queue.actions.skip(_demo_ticket(queue, 10, "Onsite Data Analyst", "Example Mill"))

    cancelled = _demo_ticket(queue, 11, "Duplicate posting", "Example Logistics")
    _force_status(queue, cancelled, "cancelled", "duplicate of TKT-0001")

    queue.clear_notifications()


class AppApi:
    """Methods exposed to the UI (pywebview ``js_api`` or the HTTP fallback).

    Attributes are underscore-prefixed so pywebview does not expose them.
    """

    def __init__(self, queue: OutboxQueue):
        self._queue = queue

    @staticmethod
    def _controls(status: str) -> dict:
        return {
            "can_approve": status in APPROVABLE,
            "can_skip": "skipped" in TRANSITIONS.get(status, set()),
        }

    def _card(self, row: dict) -> dict:
        payload = self._queue.get(row["id"])["payload"]
        return {
            **row,
            "preferred_worker": payload.get("preferred_worker"),
            "handoff_worker": (payload.get("handoff") or {}).get("worker"),
            **self._controls(row["status"]),
        }

    def board(self) -> dict:
        lanes: dict[str, list[dict]] = {s: [] for s in BOARD_STATUSES + SIDE_LANE_STATUSES}
        for row in self._queue.list(include_terminal=True):
            lanes.setdefault(row["status"], []).append(self._card(row))
        return {
            "columns": [{"status": s, "tickets": lanes[s]} for s in BOARD_STATUSES],
            "side_lane": [{"status": s, "tickets": lanes[s]} for s in SIDE_LANE_STATUSES],
            "counts": self._queue.count_by_status(),
            "gate": self.gate(),
            "notifier": self.notifier(),
        }

    def ticket(self, ticket_id: str) -> dict:
        try:
            ticket = self._queue.get(ticket_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "ticket": {**ticket, **self._controls(ticket["status"])},
            "history": self._queue.history(ticket["id"]),
            "files": self._files(ticket),
        }

    def _files(self, ticket: dict) -> list[dict]:
        payload = ticket.get("payload") or {}
        entries = sorted((payload.get("artifacts") or {}).items())
        packet = (payload.get("handoff") or {}).get("packet_path")
        if packet:
            entries.append(("handoff packet", packet))
        return [self._file_info(label, path) for label, path in entries]

    def _file_info(self, label: str, raw: str) -> dict:
        info = {"label": label, "path": str(raw), "exists": False, "size": None, "preview": None}
        path = Path(raw).resolve()
        if not path.is_relative_to(self._queue.state):
            info["note"] = "outside the demo state dir; not read"
            return info
        if not path.is_file():
            return info
        with path.open("rb") as fh:
            data = fh.read(PREVIEW_BYTES + 1)
        info.update(
            exists=True,
            size=path.stat().st_size,
            preview=data[:PREVIEW_BYTES].decode("utf-8", "replace"),
            truncated=len(data) > PREVIEW_BYTES,
        )
        return info

    def approve(self, ticket_id: str, worker: str) -> dict:
        return self._act(self._queue.actions.approve, ticket_id, worker)

    def skip(self, ticket_id: str) -> dict:
        return self._act(self._queue.actions.skip, ticket_id)

    @staticmethod
    def _act(fn, *args) -> dict:
        try:
            return fn(*args)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def gate(self) -> dict:
        cfg = self._queue.cfg
        is_open = apply_gate_open(cfg)
        return {
            "apply_opens": str(cfg.get("apply_opens")),
            "open": is_open,
            "mode": "apply" if is_open else "review_only",
            "today": date.today().isoformat(),
        }

    def notifier(self) -> dict:
        return {
            "channel": "outbox",
            "sends": False,
            "configured": os.environ.get("HITL_NOTIFY") or "stdout",
            "muted": bool(self._queue.cfg.get("mute_notifications")),
            "outbox": self._queue.recent_notifications(),
        }

    def set_mute(self, muted: bool) -> dict:
        self._queue.cfg["mute_notifications"] = bool(muted)
        return self.notifier()

    def _release_thread(self) -> None:
        self._queue.close()


def render_page(transport: str, token: str = "") -> str:
    boot = json.dumps({"transport": transport, "token": token})
    return PAGE.read_text(encoding="utf-8").replace("/*__HITL_BOOT__*/null", boot)


def make_http_handler(api: AppApi, token: str, page: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, code: int, message: str) -> None:
            self._send(code, json.dumps({"ok": False, "error": message}))

        def _host_ok(self) -> bool:
            # Rejects DNS-rebinding requests that would otherwise read the token.
            port = self.server.server_address[1]
            return (self.headers.get("Host") or "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self) -> None:
            if not self._host_ok():
                self._error(403, "bad host")
            elif self.path in {"/", "/index.html"}:
                self._send(200, page, "text/html; charset=utf-8")
            else:
                self._error(404, "not found")

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._error(403, "bad host")
            name = self.path.removeprefix("/api/")
            if not self.path.startswith("/api/") or name not in API_METHODS:
                return self._error(404, "not found")
            got = (self.headers.get("X-Hitl-Token") or "").encode("utf-8")
            if not secrets.compare_digest(got, token.encode("utf-8")):
                return self._error(401, "unauthorized")
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                args = json.loads(raw.decode("utf-8") or "{}").get("args") or []
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                return self._error(400, "invalid JSON")
            if not isinstance(args, list):
                return self._error(400, "args must be a list")
            try:
                result = getattr(api, name)(*args)
            except TypeError as exc:
                return self._error(400, str(exc))
            finally:
                api._release_thread()
            self._send(200, json.dumps(result, default=str))

    return Handler


def make_server(api: AppApi, *, port: int = 0) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)
    handler = make_http_handler(api, token, render_page("http", token))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def _load_webview():
    try:
        import webview
    except ImportError:
        print(
            "pywebview is not installed (pip install -e \".[app]\"); "
            "opening the board in your browser instead."
        )
        return None
    return webview


def _run_webview(webview, api: AppApi) -> None:
    webview.create_window(
        "HITL queue - demo board",
        html=render_page("pywebview"),
        js_api=api,
        width=1440,
        height=900,
        min_size=(1000, 640),
    )
    webview.start()


def _run_browser(api: AppApi, *, port: int = 0, open_browser: bool = True) -> int:
    httpd = make_server(api, port=port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"HITL demo board at {url}  (Ctrl-C to quit)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def launch(
    *,
    force_browser: bool = False,
    port: int = 0,
    apply_opens: str | None = None,
    open_browser: bool = True,
) -> int:
    config = demo_config(apply_opens)
    with tempfile.TemporaryDirectory(prefix="hitl-app-demo-") as tmp:
        queue = OutboxQueue(Path(tmp), config=config)
        try:
            seed_demo(queue)
            api = AppApi(queue)
            webview = None if force_browser else _load_webview()
            if webview is not None:
                try:
                    _run_webview(webview, api)
                    return 0
                except Exception as exc:
                    print(f"pywebview could not start ({exc}); opening in your browser instead.")
            return _run_browser(api, port=port, open_browser=open_browser)
        finally:
            queue.close()
