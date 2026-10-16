"""Cross-platform tray/menu-bar UI for the HITL queue.

The read model in this module deliberately has no dependency on pystray or a
desktop display.  The small adapter at the bottom imports UI packages only
when the tray is actually launched.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .queue import Queue
from .text import sanitize_summary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrayItem:
    """Toolkit-neutral menu item; children make the item a submenu."""

    text: str
    action: Callable[[], object] | None = None
    children: tuple["TrayItem", ...] = ()
    enabled: bool = True


class TrayModel:
    """Build tray state and execute menu commands using queue public APIs."""

    def __init__(
        self,
        queue: Queue,
        *,
        app_launcher: Callable[[], object] | None = None,
        on_change: Callable[[], object] | None = None,
    ) -> None:
        self.queue = queue
        self.app_launcher = app_launcher or launch_desktop_app
        self.on_change = on_change
        self._lock = threading.RLock()
        self._refresh_error: str | None = None
        self._action_error: str | None = None
        self._items: tuple[TrayItem, ...] = ()
        self.refresh()

    @property
    def items(self) -> tuple[TrayItem, ...]:
        with self._lock:
            return self._items

    @property
    def refresh_error(self) -> str | None:
        with self._lock:
            return self._refresh_error

    @property
    def action_error(self) -> str | None:
        with self._lock:
            return self._action_error

    def refresh(self) -> tuple[TrayItem, ...]:
        """Re-read all state. Errors become a menu row instead of killing polling."""
        try:
            approvals = self._approval_items()
            running = self._running_items()
            usage = self._usage_items()
            refresh_error = None
        except Exception as exc:
            refresh_error = str(exc)
            approvals = running = usage = ()

        sections: list[TrayItem] = [
            TrayItem("Pending approvals", children=approvals or (self._empty("None"),)),
            TrayItem("Running agents", children=running or (self._empty("None"),)),
            TrayItem("Usage", children=usage or (self._empty("No usage plans"),)),
        ]
        with self._lock:
            self._refresh_error = refresh_error
            errors = tuple(error for error in (self._action_error, refresh_error) if error)
        for error in errors:
            clean_error = sanitize_summary(
                error, ellipsis=True, escape_ampersand=sys.platform == "win32"
            )
            sections.append(self._empty(f"Error: {clean_error}"))
        sections.extend((
            TrayItem("Open app", self.open_app),
            TrayItem("Refresh", self.refresh),
            TrayItem("Quit"),
        ))
        with self._lock:
            self._items = tuple(sections)
            return self._items

    def _approval_items(self) -> tuple[TrayItem, ...]:
        items = []
        for ticket in self.queue.list(status="review"):
            worker = self._worker_for(ticket["id"])
            title = sanitize_summary(
                ticket["title"], limit=60, ellipsis=True,
                escape_ampersand=sys.platform == "win32",
            )
            items.append(TrayItem(
                f"{ticket['id']} — {title}",
                children=(
                    TrayItem(f"Approve → {worker}", self._approve(ticket["id"], worker)),
                    TrayItem("Skip", self._skip(ticket["id"])),
                ),
            ))
        return tuple(items)

    def _worker_for(self, ticket_id: str) -> str:
        ticket = self.queue.get(ticket_id)
        preferred = (ticket.get("payload") or {}).get("preferred_worker")
        worker = str(preferred or self.queue.cfg.get("default_apply_worker", "codex")).lower()
        return worker if worker in {"codex", "claude"} else "codex"

    def _running_items(self) -> tuple[TrayItem, ...]:
        return tuple(self._empty(
            f"{run['agent']} — {run['thread_id']} ({run['status']})"
        ) for run in self.queue.threads.running())

    def _usage_items(self) -> tuple[TrayItem, ...]:
        rows = []
        for summary in self.queue.usage.summary():
            percentages = [
                value for value in summary.get("percent_used", {}).values()
                if value is not None
            ]
            percent = max(percentages, default=0.0)
            marker = " BLOCKED" if summary.get("blocked") else " WARN" if summary.get("warn") else ""
            rows.append(self._empty(
                f"{summary['agent']} {usage_bar(percent)} {percent:.0f}%{marker}"
            ))
        return tuple(rows)

    def _approve(self, ticket_id: str, worker: str) -> Callable[[], object]:
        return lambda: self._run_action(self.queue.actions.approve, ticket_id, worker)

    def _skip(self, ticket_id: str) -> Callable[[], object]:
        return lambda: self._run_action(self.queue.actions.skip, ticket_id)

    def _run_action(self, action: Callable, *args) -> object:
        try:
            result = action(*args)
            if isinstance(result, dict) and result.get("ok") is False:
                error = result.get("error") or result.get("message") or "action failed"
                with self._lock:
                    self._action_error = str(error)
            else:
                with self._lock:
                    self._action_error = None
        except Exception as exc:
            with self._lock:
                self._action_error = str(exc)
            result = None
        self.refresh()
        if self.on_change:
            self.on_change()
        return result

    def open_app(self) -> object:
        return self.app_launcher()

    @staticmethod
    def _empty(text: str) -> TrayItem:
        return TrayItem(text, enabled=False)


def usage_bar(percent: float, width: int = 10) -> str:
    """Return a fixed-width, clamped textual quota bar."""
    bounded = min(100.0, max(0.0, float(percent)))
    filled = int(bounded * width / 100.0 + 0.5)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def launch_desktop_app() -> subprocess.Popen:
    """Open the existing desktop app in a separate process."""
    command = [sys.executable, "app"] if getattr(sys, "frozen", False) else [
        sys.executable, "-m", "hitl_queue", "app"
    ]
    return subprocess.Popen(command)


def make_icon_image(size: int = 64):
    """Generate the tray icon in memory; no binary package asset is needed."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = max(2, size // 16)
    draw.rounded_rectangle((pad, pad, size - pad, size - pad), radius=size // 5,
                           fill=(38, 99, 235, 255))
    width = max(3, size // 11)
    draw.line((size * .24, size * .52, size * .43, size * .70), fill="white", width=width)
    draw.line((size * .42, size * .70, size * .77, size * .31), fill="white", width=width)
    return image


class TrayApplication:
    """Thin pystray adapter with a background polling loop."""

    def __init__(
        self,
        model: TrayModel,
        *,
        poll_interval: float = 5.0,
        schedule_update: Callable[[Callable[[], None]], object] | None = None,
        join_timeout: float = 2.0,
    ) -> None:
        import pystray

        self.pystray = pystray
        self.model = model
        self.poll_interval = poll_interval
        self.schedule_update = schedule_update or schedule_on_main_thread
        self.join_timeout = join_timeout
        self.stopped = threading.Event()
        self.poll_thread: threading.Thread | None = None
        self.icon = pystray.Icon("hitl-queue", make_icon_image(), "HITL Queue")
        self.model.on_change = self.request_menu_update
        self.update_menu()

    def _menu(self, items: Sequence[TrayItem]):
        entries = []
        for item in items:
            if item.text == "Quit" and item.action is None:
                callback = self.quit
            elif item.action is not None:
                callback = lambda _icon, _entry, fn=item.action: self._invoke(fn)
            else:
                callback = None
            submenu = self._menu(item.children) if item.children else None
            entries.append(self.pystray.MenuItem(
                item.text, submenu or callback, enabled=item.enabled
            ))
        return self.pystray.Menu(*entries)

    def _invoke(self, action: Callable[[], object]) -> object:
        try:
            return action()
        finally:
            self.update_menu()

    def update_menu(self) -> None:
        self.icon.menu = self._menu(self.model.items)
        if getattr(self.icon, "visible", False):
            self.icon.update_menu()

    def request_menu_update(self) -> object:
        return self.schedule_update(self.update_menu)

    def _poll(self) -> None:
        while not self.stopped.wait(self.poll_interval):
            self.model.refresh()
            self.request_menu_update()
        self.model.queue.close_thread()

    def quit(self, _icon=None, _item=None) -> None:
        self.stopped.set()
        thread = self.poll_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.join_timeout)
            if thread.is_alive():
                logger.warning(
                    "Tray poll thread did not stop within %.1f seconds; "
                    "deferring queue cleanup",
                    self.join_timeout,
                )
        self.icon.stop()

    def run(self) -> None:
        self.poll_thread = threading.Thread(
            target=self._poll, name="hitl-tray-poll", daemon=True
        )
        self.poll_thread.start()
        try:
            self.icon.run()
        finally:
            self.stopped.set()
            self.poll_thread.join(self.join_timeout)
            if self.poll_thread.is_alive():
                logger.warning(
                    "Tray poll thread did not stop within %.1f seconds; "
                    "deferring queue cleanup",
                    self.join_timeout,
                )


def schedule_on_main_thread(callback: Callable[[], None]) -> object:
    """Marshal Cocoa UI work to its main loop when PyObjC provides a helper."""
    if sys.platform == "darwin":
        try:
            from PyObjCTools import AppHelper

            return AppHelper.callAfter(callback)
        except (ImportError, AttributeError):
            pass
    return callback()


def user_data_dir() -> Path:
    """Return the platform's conventional per-user application data directory."""
    import os

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HITL Queue"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / "HITL Queue"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "hitl-queue"


def launch(*, state_dir: str | Path | None = None, demo: bool = False) -> int:
    """Run the tray against public state, or existing synthetic demo data."""
    temporary = tempfile.TemporaryDirectory(prefix="hitl-tray-demo-") if demo else None
    if demo:
        from .app import OutboxQueue, demo_config, seed_demo

        queue = OutboxQueue(Path(temporary.name), config=demo_config())
        seed_demo(queue)
    else:
        frozen_dir = user_data_dir() if getattr(sys, "frozen", False) else None
        resolved_state = Path(state_dir) if state_dir is not None else (
            frozen_dir / "state" if frozen_dir else Path("state")
        )
        queue = Queue(resolved_state)
        agents_file = frozen_dir / "agents.toml" if frozen_dir else Path("agents.toml")
        if agents_file.is_file():
            queue.threads.configure_from(agents_file)
    application = None
    try:
        application = TrayApplication(TrayModel(queue))
        application.run()
        return 0
    finally:
        poll_thread = application.poll_thread if application is not None else None
        if poll_thread is not None and poll_thread.is_alive():
            logger.warning("Leaving queue open because the tray poll thread is still running")
        else:
            queue.close()
            if temporary:
                temporary.cleanup()
