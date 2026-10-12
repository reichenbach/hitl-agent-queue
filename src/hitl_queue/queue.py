"""Durable SQLite work queue for multi-agent handoffs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from .notify import dispatch, get_notifier, progress_text

STATUSES = {
    "new",
    "drafting",
    "review",
    "approved",
    "handoff",
    "done",
    "blocked",
    "skipped",
    "cancelled",
}
TERMINAL = {"done", "skipped", "cancelled"}
KINDS = {"task", "job_draft", "site", "general"}

TRANSITIONS = {
    "new": {"drafting", "skipped", "cancelled", "blocked"},
    "drafting": {"review", "blocked", "cancelled", "new", "done", "skipped"},
    "review": {"approved", "drafting", "skipped", "cancelled", "blocked", "done"},
    "approved": {"handoff", "review", "cancelled", "blocked", "done", "skipped"},
    "handoff": {"done", "blocked", "cancelled"},
    "blocked": {"new", "drafting", "review", "approved", "cancelled", "skipped"},
    "skipped": set(),
    "cancelled": set(),
    "done": set(),
}

DEFAULT_CONFIG = {
    "apply_opens": "2099-01-01",
    "default_apply_worker": "codex",
    "notify_progress": True,
    "mute_notifications": False,
    "progress_events": [
        "drafting",
        "approved",
        "handoff",
        "done",
        "skipped",
        "cancelled",
        "blocked",
    ],
}


def load_config(path: Path | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path and path.is_file():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def apply_gate_open(cfg: dict | None = None, *, today: date | None = None) -> bool:
    cfg = cfg or DEFAULT_CONFIG
    return (today or date.today()) >= date.fromisoformat(str(cfg.get("apply_opens")))


class Queue:
    def __init__(self, state_dir: Path, *, config: dict | None = None, notifier=None):
        self.state = Path(state_dir).resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cfg = config or dict(DEFAULT_CONFIG)
        self.notifier = notifier or get_notifier()
        self._connections = threading.local()
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tickets(
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              status TEXT NOT NULL,
              title TEXT NOT NULL,
              company TEXT,
              url TEXT,
              owner TEXT,
              payload TEXT NOT NULL,
              created REAL NOT NULL,
              updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              ticket_id TEXT NOT NULL,
              status TEXT NOT NULL,
              actor TEXT NOT NULL,
              note TEXT,
              at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

        # Imported lazily to keep the action layer independent of construction.
        from .actions import Actions

        self.actions = Actions(self)

    @property
    def db(self) -> sqlite3.Connection:
        """Return this thread's connection; Queue instances can be shared safely."""
        connection = getattr(self._connections, "db", None)
        if connection is None:
            connection = sqlite3.connect(
                self.state / "queue.sqlite3", timeout=30, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            self._connections.db = connection
        return connection

    def close(self) -> None:
        connection = getattr(self._connections, "db", None)
        if connection is not None:
            connection.close()
            del self._connections.db

    def _notify(self, text: str, *, source: str) -> dict:
        return dispatch(
            self.notifier,
            text,
            source=source,
            mute=bool(self.cfg.get("mute_notifications", False)),
        )

    def transaction(self, fn):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = fn()
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _event(self, tid: str, status: str, actor: str, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(ticket_id,status,actor,note,at) VALUES(?,?,?,?,?)",
            (tid, status, actor, note, time.time()),
        )

    def _next_id(self) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key='next_seq'").fetchone()
        n = int(row["value"]) if row else 1
        self.db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('next_seq',?)",
            (str(n + 1),),
        )
        return f"TKT-{n:04d}"

    def get(self, tid: str) -> dict:
        row = self.db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not row:
            raise ValueError(f"Unknown ticket {tid}")
        obj = dict(row)
        obj["payload"] = json.loads(obj["payload"] or "{}")
        return obj

    def list(self, *, status: str | None = None, include_terminal: bool = False) -> list[dict]:
        sql = "SELECT id,kind,status,title,company,url,owner,created,updated FROM tickets WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if not include_terminal and not status:
            sql += " AND status NOT IN ('done','skipped','cancelled')"
        sql += " ORDER BY created"
        return [dict(r) for r in self.db.execute(sql, args)]

    def history(self, tid: str) -> list[dict]:
        """Return a ticket's status events in chronological order."""
        self.get(tid)  # Give callers the same unknown-ticket error as other helpers.
        rows = self.db.execute(
            "SELECT seq,ticket_id,status,actor,note,at FROM events "
            "WHERE ticket_id=? ORDER BY seq",
            (tid,),
        )
        return [dict(row) for row in rows]

    status_history = history

    def count_by_status(self) -> dict[str, int]:
        """Count all tickets, including terminal tickets, grouped by status."""
        counts = {status: 0 for status in STATUSES}
        for row in self.db.execute(
            "SELECT status, COUNT(*) AS count FROM tickets GROUP BY status"
        ):
            counts[row["status"]] = row["count"]
        return counts

    status_counts = count_by_status

    def _ping(self, ticket: dict, status: str, detail: str = "") -> None:
        if not self.cfg.get("notify_progress", True):
            return
        if status not in (self.cfg.get("progress_events") or []):
            return
        self._notify(
            progress_text(ticket, status, detail=detail),
            source="hitl-progress",
        )

    def enqueue(
        self,
        *,
        title: str,
        company: str = "",
        url: str = "",
        kind: str = "task",
        reason: str = "",
        actor: str = "dispatcher",
    ) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if not title.strip():
            raise ValueError("title required")

        def action():
            tid = self._next_id()
            stamp = time.time()
            payload = {"reason": reason, "artifacts": {}, "notes": ""}
            self.db.execute(
                "INSERT INTO tickets(id,kind,status,title,company,url,owner,payload,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    kind,
                    "new",
                    title.strip(),
                    company.strip(),
                    url.strip() or None,
                    None,
                    json.dumps(payload, sort_keys=True),
                    stamp,
                    stamp,
                ),
            )
            self._event(tid, "new", actor, reason)
            return self.get(tid)

        ticket = self.transaction(action)
        self._notify(
            f"New {ticket['id']}\n{ticket.get('company') or ''} - {ticket['title']}\n"
            f"Queued for draft.\nSkip: SKIP {ticket['id']}",
            source="hitl-new",
        )
        return ticket

    def claim(self, tid: str | None, worker: str) -> dict:
        def action():
            if tid:
                row = self.db.execute(
                    "SELECT id,status FROM tickets WHERE id=?", (tid,)
                ).fetchone()
            else:
                row = self.db.execute(
                    "SELECT id,status FROM tickets WHERE status='new' ORDER BY created LIMIT 1"
                ).fetchone()
            if not row:
                return {"ticket": None}
            if row["status"] != "new":
                raise ValueError(f"{row['id']} is {row['status']}, not new")
            jid = row["id"]
            self.db.execute(
                "UPDATE tickets SET status='drafting', owner=?, updated=? WHERE id=?",
                (worker, time.time(), jid),
            )
            self._event(jid, "drafting", worker, f"claimed by {worker}")
            return {"ticket": self.get(jid)}

        result = self.transaction(action)
        if result.get("ticket"):
            self._ping(result["ticket"], "drafting", detail=f"claimed by {worker}")
        return result

    def _set(self, tid: str, status: str, actor: str, note: str = "", payload_update: dict | None = None) -> dict:
        job = self.get(tid)
        allowed = TRANSITIONS.get(job["status"], set())
        if status not in allowed:
            raise ValueError(f"Cannot move {tid} {job['status']} → {status}")
        payload = job["payload"]
        if payload_update:
            arts = payload.setdefault("artifacts", {})
            if "artifacts" in payload_update:
                arts.update(payload_update.pop("artifacts") or {})
            payload.update(payload_update)
        self.db.execute(
            "UPDATE tickets SET status=?, payload=?, updated=? WHERE id=?",
            (status, json.dumps(payload, sort_keys=True), time.time(), tid),
        )
        self._event(tid, status, actor, note)
        return self.get(tid)

    def submit_draft(
        self,
        tid: str,
        *,
        actor: str,
        resume: str = "",
        cover_letter: str = "",
        research_summary: str = "",
        note: str = "",
    ) -> dict:
        arts = {}
        if resume:
            arts["resume"] = resume
        if cover_letter:
            arts["cover_letter"] = cover_letter
        preferred = (self.get(tid).get("payload") or {}).get("preferred_worker")

        def action():
            update = {
                "artifacts": arts,
                "research_summary": research_summary,
            }
            if preferred:
                update["preferred_worker"] = preferred
            return self._set(
                tid,
                "review",
                actor,
                note or "draft ready",
                payload_update=update,
            )

        ticket = self.transaction(action)
        self._notify(
            f"{tid} ready\n{ticket.get('company') or ''} - {ticket['title']}\n"
            f"Reply:\nAPPROVE {tid} CODEX\nAPPROVE {tid} CLAUDE\nSKIP {tid}",
            source="hitl-review",
        )
        if preferred in {"codex", "claude"}:
            self.approve(tid, actor="auto", note=f"preferred_worker={preferred}")
            mode = "apply" if apply_gate_open(self.cfg) else "review_only"
            return self.handoff(tid, to=preferred, actor="auto", mode=mode)
        return ticket

    def approve(self, tid: str, *, actor: str = "human", note: str = "") -> dict:
        def action():
            return self._set(tid, "approved", actor, note or "approved")

        ticket = self.transaction(action)
        self._ping(ticket, "approved", detail=note or actor)
        return ticket

    def skip(self, tid: str, *, actor: str = "human", note: str = "") -> dict:
        def action():
            return self._set(tid, "skipped", actor, note or "skipped")

        ticket = self.transaction(action)
        self._ping(ticket, "skipped", detail=note or actor)
        return ticket

    def set_preferred_worker(self, tid: str, worker: str) -> dict:
        worker = worker.lower().strip()
        if worker not in {"codex", "claude"}:
            raise ValueError("worker must be codex or claude")

        def action():
            job = self.get(tid)
            payload = job["payload"]
            payload["preferred_worker"] = worker
            self.db.execute(
                "UPDATE tickets SET payload=?, updated=? WHERE id=?",
                (json.dumps(payload, sort_keys=True), time.time(), tid),
            )
            self._event(tid, job["status"], "human", f"preferred_worker={worker}")
            return self.get(tid)

        return self.transaction(action)

    def handoff(
        self,
        tid: str,
        *,
        to: str,
        actor: str = "human",
        mode: str = "review_only",
    ) -> dict:
        to = to.lower()
        if to not in {"codex", "claude"}:
            raise ValueError("to must be codex or claude")
        if mode not in {"apply", "review_only"}:
            raise ValueError("mode must be apply or review_only")
        ticket = self.get(tid)
        if ticket["status"] != "approved":
            raise ValueError(f"{tid} must be approved")
        packet = {
            "id": tid,
            "title": ticket["title"],
            "company": ticket.get("company"),
            "url": ticket.get("url"),
            "mode": mode,
            "worker": to,
            "artifacts": (ticket.get("payload") or {}).get("artifacts") or {},
            "research_summary": (ticket.get("payload") or {}).get("research_summary") or "",
        }
        packet_path = self.state / f"{tid}-packet.json"
        packet_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")

        def action():
            return self._set(
                tid,
                "handoff",
                actor,
                f"{to} mode={mode}",
                payload_update={
                    "handoff": {
                        "worker": to,
                        "mode": mode,
                        "packet_path": str(packet_path),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                },
            )

        ticket = self.transaction(action)
        self._ping(ticket, "handoff", detail=f"{to} packet ready; mode={mode}")
        return ticket

    def done(self, tid: str, *, actor: str, note: str = "") -> dict:
        def action():
            return self._set(tid, "done", actor, note or "done")

        ticket = self.transaction(action)
        self._ping(ticket, "done", detail=note or actor)
        return ticket

    def handle_reply(self, text: str) -> dict:
        """Parse one human reply line and act."""
        import re

        line = (text or "").strip().splitlines()[0] if text else ""
        m = re.match(
            r"^\s*(APPROVE|YES|GO)\s+(TKT-\d+)\s+(CODEX|CLAUDE)\s*$",
            line,
            re.I,
        )
        if m:
            tid, worker = m.group(2).upper(), m.group(3).lower()
            return self.actions.approve(tid, worker)

        m = re.match(r"^\s*(SKIP|DENY|NO)\s+(TKT-\d+)\s*$", line, re.I)
        if m:
            tid = m.group(2).upper()
            return self.actions.skip(tid)

        return {"ok": False, "error": "unparsed"}
