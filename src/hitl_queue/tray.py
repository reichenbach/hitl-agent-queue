"""Cross-platform tray/menu-bar UI for the HITL queue.

The read model in this module deliberately has no dependency on pystray or a
desktop display.  The small adapter at the bottom imports UI packages only
when the tray is actually launched.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .queue import Queue


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
        self.last_error: str | None = None
        self._items: tuple[TrayItem, ...] = ()
        self.refresh()

    @property
    def items(self) -> tuple[TrayItem, ...]:
        return self._items

    def refresh(self) -> tuple[TrayItem, ...]:
        """Re-read all state. Errors become a menu row instead of killing polling."""
        try:
            approvals = self._approval_items()
            running = self._running_items()
            usage = self._usage_items()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            approvals = running = usage = ()

        sections: list[TrayItem] = [
            TrayItem("Pending approvals", children=approvals or (self._empty("None"),)),
            TrayItem("Running agents", children=running or (self._empty("None"),)),
            TrayItem("Usage", children=usage or (self._empty("No usage plans"),)),
        ]
        if self.last_error:
            sections.append(self._empty(f"Error: {self.last_error}"))
        sections.extend((
            TrayItem("Open app", self.open_app),
            TrayItem("Refresh", self.refresh),
            TrayItem("Quit"),
        ))
        self._items = tuple(sections)
        return self._items

    def _approval_items(self) -> tuple[TrayItem, ...]:
        items = []
        for ticket in self.queue.list(status="review"):
            worker = self._worker_for(ticket["id"])
            items.append(TrayItem(
                f"{ticket['id']} — {ticket['title']}",
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
        except Exception as exc:
            self.last_error = str(exc)
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

    def __init__(self, model: TrayModel, *, poll_interval: float = 5.0) -> None:
        import pystray

        self.pystray = pystray
        self.model = model
        self.poll_interval = poll_interval
        self.stopped = threading.Event()
        self.icon = pystray.Icon("hitl-queue", make_icon_image(), "HITL Queue")
        self.model.on_change = self.update_menu
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

    def _poll(self) -> None:
        while not self.stopped.wait(self.poll_interval):
            self.model.refresh()
            self.update_menu()
        self.model.queue.close_thread()

    def quit(self, _icon=None, _item=None) -> None:
        self.stopped.set()
        self.icon.stop()

    def run(self) -> None:
        threading.Thread(target=self._poll, name="hitl-tray-poll", daemon=True).start()
        self.icon.run()


def launch(*, state_dir: str | Path = "state", demo: bool = False) -> int:
    """Run the tray against public state, or existing synthetic demo data."""
    temporary = tempfile.TemporaryDirectory(prefix="hitl-tray-demo-") if demo else None
    if demo:
        from .app import OutboxQueue, demo_config, seed_demo

        queue = OutboxQueue(Path(temporary.name), config=demo_config())
        seed_demo(queue)
    else:
        queue = Queue(Path(state_dir))
        agents_file = Path("agents.toml")
        if agents_file.is_file():
            queue.threads.configure_from(agents_file)
    try:
        TrayApplication(TrayModel(queue)).run()
        return 0
    finally:
        queue.close()
        if temporary:
            temporary.cleanup()
