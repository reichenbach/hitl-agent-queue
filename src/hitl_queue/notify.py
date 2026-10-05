"""Notifiers: stdout (default) or optional Cortland iMessage CLI."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

PROGRESS_LABELS = {
    "new": "queued",
    "drafting": "drafting",
    "review": "draft ready (review)",
    "approved": "approved",
    "handoff": "handed off",
    "done": "done",
    "skipped": "skipped",
    "cancelled": "cancelled",
    "blocked": "blocked",
}


def plain(s: str) -> str:
    return (
        (s or "")
        .replace("—", "-")
        .replace("–", "-")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
    )


def progress_text(ticket: dict, status: str, *, detail: str = "") -> str:
    company = plain(ticket.get("company") or "Unknown")
    title = plain(ticket.get("title") or "")
    tid = ticket.get("id") or "?"
    label = PROGRESS_LABELS.get(status, status)
    line = f"{tid} {label}\n{company} - {title}"
    if detail:
        line += "\n" + plain(re.sub(r"\s+", " ", detail)[:160])
    preferred = (ticket.get("payload") or {}).get("preferred_worker")
    if preferred and status in {"new", "drafting", "review"}:
        line += f"\nWorker pick: {preferred}"
    return line


class StdoutNotifier:
    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        body = text.strip()
        print(f"[{source}] {body}")
        return {"ok": True, "channel": "stdout"}


class CortlandNotifier:
    """Optional. Requires a local cortland-imessage CLI."""

    def __init__(self, cli: Path | None = None, node: str | None = None):
        self.cli = Path(
            cli
            or os.environ.get(
                "CORTLAND_IMESSAGE_CLI",
                "",
            )
        )
        self.node = node or os.environ.get("NODE_BIN", "node")

    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        if not self.cli.is_file():
            return {"ok": False, "error": "cortland CLI missing; set CORTLAND_IMESSAGE_CLI"}
        body = text.strip()
        if len(body) > 450:
            body = body[:447] + "..."
        proc = subprocess.run(
            [self.node, str(self.cli), "notify", "--text", body, "--source", source],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "ok": proc.returncode == 0,
            "channel": "cortland",
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "code": proc.returncode,
        }


def get_notifier():
    mode = (os.environ.get("HITL_NOTIFY") or "stdout").lower()
    if mode == "cortland":
        return CortlandNotifier()
    return StdoutNotifier()
