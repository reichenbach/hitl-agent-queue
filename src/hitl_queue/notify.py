"""Pluggable notifiers: stdout, Cortland, Slack, Teams, Discord, generic webhook."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

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


class Notifier(Protocol):
    def notify(self, text: str, *, source: str = "hitl-queue") -> dict: ...


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def dispatch(
    notifier: Notifier,
    text: str,
    *,
    source: str = "hitl-queue",
    mute: bool | None = None,
) -> dict:
    """Send one notification and report the selected channel and outcome."""
    muted = _env_truthy("HITL_NOTIFY_MUTE") if mute is None else mute
    if muted:
        return {"channel": "muted", "ok": True, "error": None}

    try:
        result = dict(notifier.notify(text, source=source) or {})
    except Exception as exc:
        return {
            "channel": notifier.__class__.__name__.removesuffix("Notifier").lower(),
            "ok": False,
            "error": str(exc),
        }
    result.setdefault("channel", notifier.__class__.__name__.removesuffix("Notifier").lower())
    result["ok"] = bool(result.get("ok"))
    result.setdefault("error", None)
    return result


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


def _post_json(url: str, payload: dict, *, timeout: int = 20) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return {"ok": True, "status": getattr(resp, "status", 200), "body": body[:500]}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")[:500]
        return {"ok": False, "status": exc.code, "error": err}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class StdoutNotifier:
    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        body = text.strip()
        print(f"[{source}] {body}")
        return {"ok": True, "channel": "stdout"}


class CortlandNotifier:
    """Optional. Requires a local cortland-imessage CLI."""

    def __init__(self, cli: Path | None = None, node: str | None = None):
        raw = cli or os.environ.get("CORTLAND_IMESSAGE_CLI") or ""
        self.cli = Path(raw) if raw else Path()
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


class SlackNotifier:
    """Slack Incoming Webhook. Outbound only; replies via reply-server or CLI."""

    def __init__(self, webhook_url: str | None = None):
        self.url = (webhook_url or os.environ.get("HITL_SLACK_WEBHOOK_URL") or "").strip()

    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        if not self.url:
            return {"ok": False, "error": "set HITL_SLACK_WEBHOOK_URL"}
        # Prefix source so channel history is searchable.
        payload = {"text": f"[{source}]\n{text.strip()}"}
        result = _post_json(self.url, payload)
        result["channel"] = "slack"
        return result


class TeamsNotifier:
    """Microsoft Teams Incoming Webhook (MessageCard-compatible text)."""

    def __init__(self, webhook_url: str | None = None):
        self.url = (webhook_url or os.environ.get("HITL_TEAMS_WEBHOOK_URL") or "").strip()

    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        if not self.url:
            return {"ok": False, "error": "set HITL_TEAMS_WEBHOOK_URL"}
        body = text.strip()
        # Adaptive-ish simple message works on classic and many Power Automate hooks.
        payload = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": source,
            "themeColor": "0078D4",
            "title": f"hitl-agent-queue · {source}",
            "text": body.replace("\n", "  \n"),
        }
        result = _post_json(self.url, payload)
        result["channel"] = "teams"
        return result


class DiscordNotifier:
    """Discord channel webhook."""

    def __init__(self, webhook_url: str | None = None):
        self.url = (webhook_url or os.environ.get("HITL_DISCORD_WEBHOOK_URL") or "").strip()

    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        if not self.url:
            return {"ok": False, "error": "set HITL_DISCORD_WEBHOOK_URL"}
        payload = {"content": f"**[{source}]**\n{text.strip()}"[:1900]}
        result = _post_json(self.url, payload)
        result["channel"] = "discord"
        return result


class GenericWebhookNotifier:
    """POST JSON {text, source} to any URL (n8n, Zapier, custom)."""

    def __init__(self, webhook_url: str | None = None):
        self.url = (webhook_url or os.environ.get("HITL_WEBHOOK_URL") or "").strip()

    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        if not self.url:
            return {"ok": False, "error": "set HITL_WEBHOOK_URL"}
        result = _post_json(self.url, {"text": text.strip(), "source": source})
        result["channel"] = "webhook"
        return result


class MultiNotifier:
    """Fan-out to several notifiers. Overall ok if any channel succeeds."""

    def __init__(self, notifiers: list):
        self.notifiers = list(notifiers)

    def notify(self, text: str, *, source: str = "hitl-queue") -> dict:
        results = []
        for n in self.notifiers:
            results.append(n.notify(text, source=source))
        return {
            "ok": any(r.get("ok") for r in results),
            "channel": "multi",
            "results": results,
        }


_BUILDERS = {
    "stdout": lambda: StdoutNotifier(),
    "cortland": lambda: CortlandNotifier(),
    "slack": lambda: SlackNotifier(),
    "teams": lambda: TeamsNotifier(),
    "discord": lambda: DiscordNotifier(),
    "webhook": lambda: GenericWebhookNotifier(),
}


def build_notifier(name: str):
    key = name.strip().lower()
    if key not in _BUILDERS:
        raise ValueError(
            f"Unknown notifier {name!r}. Choose: {', '.join(sorted(_BUILDERS))}"
        )
    return _BUILDERS[key]()


def get_notifier():
    """
    HITL_NOTIFY selects the channel:
      stdout (default) | cortland | slack | teams | discord | webhook
      multi  → HITL_NOTIFY_CHANNELS=stdout,slack,teams
    """
    mode = (os.environ.get("HITL_NOTIFY") or "stdout").lower().strip()
    if mode == "multi":
        names = [
            n.strip()
            for n in (os.environ.get("HITL_NOTIFY_CHANNELS") or "stdout").split(",")
            if n.strip()
        ]
        return MultiNotifier([build_notifier(n) for n in names])
    return build_notifier(mode)
