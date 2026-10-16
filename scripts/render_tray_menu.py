#!/usr/bin/env python3
"""Render the tray menu as a standalone HTML page for headless screenshots.

Seeds a throwaway state dir with neutral demo data, builds the real
``hitl_queue.tray.TrayModel`` from it, and writes an HTML file that draws
exactly those menu items as a light macOS-style menu with every submenu
expanded.  No pystray, Pillow, or UI is imported or opened.

    python3 scripts/render_tray_menu.py [--output PATH]

Screenshot the ``#capture`` element (or the full page) of the printed file.
"""

from __future__ import annotations

import argparse
import html
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hitl_queue.agents import AgentLimits, FakeAgent, UsagePlan  # noqa: E402
from hitl_queue.app import OutboxQueue, demo_config  # noqa: E402
from hitl_queue.tray import TrayItem, TrayModel  # noqa: E402

ROW_H = 22
PAD = 5
BORDER = 1
MENUBAR_H = 24
MENU_GAP = 1


class SnapshotAgent(FakeAgent):
    """Fake agent that can call a hook while its run is still in progress."""

    on_run = None

    def _run(self, prompt, cwd, cancel_event):
        if self.on_run is not None:
            self.on_run()
        return super()._run(prompt, cwd, cancel_event)


def build_items(root: Path) -> tuple[TrayItem, ...]:
    """Seed demo data and return the tray menu captured mid agent run."""
    state, work = root / "state", root / "work"
    work.mkdir(parents=True)
    queue = OutboxQueue(state, config=demo_config())
    try:
        limits = AgentLimits(allowed_folders=(work,), max_turns=4, timeout_seconds=60)
        alpha = SnapshotAgent("Alpha", limits, output="Done.", duration=240.0,
                              work_dir=work, usage_plan=UsagePlan("5h", runs=10))
        beta = SnapshotAgent("Beta", limits, output="Done.", duration=90.0, work_dir=work,
                             usage={"total_tokens": 4300},
                             usage_plan=UsagePlan("daily", tokens=10_000))
        queue.threads.configure({"alpha": alpha, "beta": beta})

        for n, title in enumerate(("Update changelog", "Triage open issues"), start=1):
            tid = queue.enqueue(title=title, url=f"https://example.com/items/{n}",
                                reason="Synthetic demo ticket", actor="demo")["id"]
            queue.claim(tid, "cursor-draft")
            queue.submit_draft(tid, actor="cursor-draft",
                               research_summary="Synthetic demo context.")
            if n == 1:
                queue.set_preferred_worker(tid, "claude")

        docs = queue.threads.create("Docs pass")["id"]
        for _ in range(3):
            queue.threads.post(docs, "demo", "@alpha task: tidy section headings")
        for _ in range(2):
            queue.threads.post(docs, "demo", "@beta task: proofread examples")

        captured: list[TrayModel] = []
        alpha.on_run = lambda: captured.append(TrayModel(queue, app_launcher=lambda: None))
        release = queue.threads.create("Release prep")["id"]
        queue.threads.post(release, "demo", "@alpha task: draft release notes")
        model = captured[0]
        if model.refresh_error:
            raise RuntimeError(f"tray model failed to read demo state: {model.refresh_error}")
        return model.items
    finally:
        queue.close()


def _row(item: TrayItem) -> str:
    classes = ["row"] + ([] if item.enabled else ["disabled"])
    chevron = '<span class="chevron">&#x203A;</span>' if item.children else ""
    return (f'<div class="{" ".join(classes)}"><span class="label">'
            f"{html.escape(item.text)}</span>{chevron}</div>")


def _panel(items: tuple[TrayItem, ...], margin_top: int) -> str:
    rows = "".join(_row(item) for item in items)
    return f'<div class="menu" style="margin-top:{margin_top}px">{rows}</div>'


def _panel_height(count: int) -> int:
    return 2 * (PAD + BORDER) + count * ROW_H


def render_columns(items: tuple[TrayItem, ...]) -> list[str]:
    """Lay out the root menu and every submenu as cascading columns."""
    columns: list[list[str]] = []
    level: list[tuple[int, tuple[TrayItem, ...]]] = [(0, items)]
    while level:
        panels, cursor = [], 0
        next_level: list[tuple[int, tuple[TrayItem, ...]]] = []
        for parent_y, children in level:
            top = max(parent_y, cursor + (MENU_GAP if panels else 0))
            panels.append(_panel(children, top - cursor))
            next_level.extend((top + index * ROW_H, child.children)
                              for index, child in enumerate(children) if child.children)
            cursor = top + _panel_height(len(children))
        columns.append(panels)
        level = next_level
    return ["".join(panels) for panels in columns]


ICON_SVG = (
    '<svg width="16" height="16" viewBox="0 0 64 64" aria-hidden="true">'
    '<rect x="4" y="4" width="56" height="56" rx="12.8" fill="#2663eb"/>'
    '<g stroke="#fff" stroke-width="5.8" stroke-linecap="round" fill="none">'
    '<line x1="15.4" y1="33.3" x2="27.5" y2="44.8"/>'
    '<line x1="26.9" y1="44.8" x2="49.3" y2="19.8"/></g></svg>'
)

STYLE = f"""
* {{ box-sizing: border-box; margin: 0; }}
html, body {{ background: #e9edf3; }}
body {{
  font: 13px/1 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue",
        Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  color: rgba(0, 0, 0, 0.85);
}}
#capture {{
  display: inline-block;
  min-width: 100%;
  padding-bottom: 28px;
  background: linear-gradient(160deg, #dfe8f5 0%, #eef1f6 55%, #f4f1ec 100%);
}}
.menubar {{
  height: {MENUBAR_H}px;
  display: flex;
  align-items: center;
  padding: 0 14px;
  background: rgba(255, 255, 255, 0.72);
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.12);
}}
.status-item {{
  display: flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.1);
}}
.cascade {{ display: flex; align-items: flex-start; padding: {MENU_GAP}px 14px 0; }}
.column {{ display: flex; flex-direction: column; align-items: stretch; margin-left: -3px; }}
.column:first-child {{ margin-left: 0; }}
.menu {{
  min-width: 180px;
  padding: {PAD}px;
  border: {BORDER}px solid rgba(0, 0, 0, 0.14);
  border-radius: 7px;
  background: rgba(246, 246, 246, 0.97);
  box-shadow: 0 0 0 0.5px rgba(255, 255, 255, 0.6) inset,
              0 8px 24px rgba(0, 0, 0, 0.16), 0 1px 3px rgba(0, 0, 0, 0.1);
}}
.row {{
  display: flex;
  align-items: center;
  height: {ROW_H}px;
  padding: 0 10px 0 14px;
  border-radius: 4px;
  white-space: pre;
}}
.row.disabled {{ color: rgba(0, 0, 0, 0.3); }}
.label {{ flex: 1; font-variant-numeric: tabular-nums; }}
.chevron {{ margin-left: 24px; font-size: 16px; color: rgba(0, 0, 0, 0.55); }}
"""


def render_html(items: tuple[TrayItem, ...]) -> str:
    columns = "".join(f'<div class="column">{col}</div>' for col in render_columns(items))
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>HITL Queue tray menu</title>"
        f"<style>{STYLE}</style></head><body>"
        '<div id="capture"><div class="menubar">'
        f'<div class="status-item">{ICON_SVG}</div></div>'
        f'<div class="cascade">{columns}</div></div>'
        "</body></html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path,
        default=Path(tempfile.gettempdir()) / "hitl-tray-menu" / "tray-menu.html",
        help="HTML file to write (default: in the system temp dir)",
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="hitl-tray-render-") as tmp:
        items = build_items(Path(tmp))
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(items), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
