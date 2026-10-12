"""Durable agent/user conversations with bounded, auditable execution."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .agents import Agent

if TYPE_CHECKING:
    from .queue import Queue


MENTION_RE = re.compile(
    r"(?im)(?:^|\s)@(?P<agent>[A-Za-z][\w.-]*)\s+(?P<kind>task|fyi)\s*:\s*(?P<body>[^\n]*)"
)
ACTION_RE = re.compile(r"(?i)(?<![\w])ACTION_REQUEST\s*:\s*")
OUTBOUND_ACTIONS = {"push", "merge", "send", "network"}
SUMMARY_LIMIT = 160


class Threads:
    """Thread API. ``post`` synchronously runs agents named by task mentions."""

    def __init__(self, queue: Queue):
        self.queue = queue
        self._agents: dict[str, Agent] = {}
        self._running: dict[int, tuple[Agent, threading.Event]] = {}
        self._lock = threading.RLock()
        self.queue.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads(
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              max_agent_runs INTEGER NOT NULL CHECK(max_agent_runs > 0),
              agent_runs INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'active',
              allow_agent_to_agent INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              thread_id TEXT NOT NULL REFERENCES threads(id),
              author_type TEXT NOT NULL CHECK(author_type IN ('user','agent')),
              author TEXT NOT NULL,
              body TEXT NOT NULL,
              created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              thread_id TEXT NOT NULL REFERENCES threads(id),
              agent TEXT NOT NULL,
              status TEXT NOT NULL,
              started REAL NOT NULL,
              ended REAL,
              ok INTEGER,
              error TEXT,
              duration REAL
            );
            CREATE TABLE IF NOT EXISTS agent_controls(
              agent TEXT PRIMARY KEY,
              state TEXT NOT NULL CHECK(state IN ('active','paused','killed'))
            );
            CREATE TABLE IF NOT EXISTS audit_log(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event TEXT NOT NULL,
              actor TEXT NOT NULL,
              thread_id TEXT,
              message_id INTEGER,
              run_id INTEGER,
              ticket_id TEXT,
              detail TEXT NOT NULL,
              at REAL NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS audit_log_no_update
            BEFORE UPDATE ON audit_log BEGIN
              SELECT RAISE(ABORT, 'audit_log is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
            BEFORE DELETE ON audit_log BEGIN
              SELECT RAISE(ABORT, 'audit_log is append-only');
            END;
            DROP TRIGGER IF EXISTS audit_log_monotonic_seq;
            CREATE TRIGGER audit_log_monotonic_seq
            AFTER INSERT ON audit_log
            WHEN NEW.seq <= COALESCE(
              (SELECT MAX(seq) FROM audit_log WHERE rowid != NEW.rowid), 0
            )
            BEGIN
              SELECT RAISE(ABORT, 'audit_log sequence must increase');
            END;
            """
        )
        columns = {row["name"] for row in self.queue.db.execute("PRAGMA table_info(threads)")}
        if "allow_agent_to_agent" not in columns:
            self.queue.db.execute(
                "ALTER TABLE threads ADD COLUMN allow_agent_to_agent INTEGER NOT NULL DEFAULT 0"
            )

    def configure(self, agents: dict[str, Agent]) -> None:
        for agent in agents.values():
            self._validate_work_dir(agent)
        self._agents = {name.casefold(): agent for name, agent in agents.items()}

    def configure_from(self, path: str | Path = "agents.toml") -> None:
        """Load adapters using the public ``hitl_queue.agents`` configuration."""
        from .agents import load_agents

        self.configure(load_agents(path))

    def create(
        self, title: str, *, max_agent_runs: int = 12, allow_agent_to_agent: bool = False
    ) -> dict:
        if not title.strip():
            raise ValueError("thread title required")
        if not isinstance(max_agent_runs, int) or max_agent_runs < 1:
            raise ValueError("max_agent_runs must be a positive integer")
        if not isinstance(allow_agent_to_agent, bool):
            raise ValueError("allow_agent_to_agent must be a boolean")

        def insert():
            row = self.queue.db.execute("SELECT value FROM meta WHERE key='next_thread_seq'").fetchone()
            number = int(row["value"]) if row else 1
            self.queue.db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('next_thread_seq',?)",
                (str(number + 1),),
            )
            thread_id = f"THR-{number:04d}"
            self.queue.db.execute(
                "INSERT INTO threads(id,title,max_agent_runs,allow_agent_to_agent,created) VALUES(?,?,?,?,?)",
                (thread_id, title.strip(), max_agent_runs, int(allow_agent_to_agent), time.time()),
            )
            return self.get(thread_id)

        return self.queue.transaction(insert)

    def get(self, thread_id: str) -> dict:
        row = self.queue.db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown thread {thread_id}")
        return dict(row)

    def messages(self, thread_id: str) -> list[dict]:
        self.get(thread_id)
        return [dict(row) for row in self.queue.db.execute(
            "SELECT * FROM thread_messages WHERE thread_id=? ORDER BY id", (thread_id,)
        )]

    def audit_log(self, thread_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit_log"
        args: tuple[str, ...] = ()
        if thread_id:
            sql += " WHERE thread_id=?"
            args = (thread_id,)
        sql += " ORDER BY seq"
        rows = []
        for row in self.queue.db.execute(sql, args):
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            rows.append(item)
        return rows

    def post(self, thread_id: str, author: str, body: str, *, cwd: str | Path | None = None) -> dict:
        if cwd is not None:
            raise ValueError("cwd overrides are not allowed; configure each agent work_dir")
        thread = self.get(thread_id)
        if thread["state"] != "active":
            raise ValueError(f"{thread_id} is {thread['state']}")
        message = self._append_message(thread_id, "user", author, body)
        replies = []
        for mention in MENTION_RE.finditer(body):
            if mention.group("kind").lower() == "fyi":
                continue
            replies.extend(self._wake(
                thread_id, mention.group("agent"), mention.group("body"), "user", author
            ))
        return {"message": message, "replies": replies}

    def _append_message(self, thread_id: str, author_type: str, author: str, body: str) -> dict:
        if not body.strip():
            raise ValueError("message body required")
        def append():
            cursor = self.queue.db.execute(
                "INSERT INTO thread_messages(thread_id,author_type,author,body,created) VALUES(?,?,?,?,?)",
                (thread_id, author_type, author, body, time.time()),
            )
            message_id = cursor.lastrowid
            self.queue.audit(
                "message", actor=author, thread_id=thread_id, message_id=message_id,
                detail={"author_type": author_type, "body": body},
            )
            return dict(self.queue.db.execute(
                "SELECT * FROM thread_messages WHERE id=?", (message_id,)
            ).fetchone())
        return self.queue.transaction(append)

    def _wake(
        self, thread_id: str, name: str, task: str, source_type: str, source_name: str
    ) -> list[dict]:
        key = name.casefold()
        agent = self._agents.get(key)
        if agent is None:
            raise ValueError(f"Unknown agent {name}")
        if source_type == "agent":
            if key == source_name.casefold():
                self.queue.audit(
                    "agent_task_rejected", actor=source_name, thread_id=thread_id,
                    detail={"reason": "self_task", "target": agent.name},
                )
                return []
            thread = self.get(thread_id)
            if not (thread["allow_agent_to_agent"] or agent.allow_agent_to_agent):
                self._create_approval_ticket(
                    thread_id, source_name, "agent_task",
                    f"Allow {source_name} to task {agent.name}: {task}",
                    {"target_agent": agent.name, "task": task},
                )
                return []
        with self._lock:
            thread = self.get(thread_id)
            if thread["state"] != "active" or self._control_state(key) != "active":
                return []
            if thread["agent_runs"] >= thread["max_agent_runs"]:
                self.queue.audit(
                    "agent_run_limit", actor=source_name, thread_id=thread_id,
                    detail={"limit": thread["max_agent_runs"], "target": agent.name},
                )
                return []
            started = time.time()

            def begin():
                self.queue.db.execute(
                    "UPDATE threads SET agent_runs=agent_runs+1 WHERE id=?", (thread_id,)
                )
                cursor = self.queue.db.execute(
                    "INSERT INTO agent_runs(thread_id,agent,status,started) VALUES(?,?,?,?)",
                    (thread_id, agent.name, "running", started),
                )
                run_id = cursor.lastrowid
                self.queue.audit("agent_run_start", actor=agent.name, thread_id=thread_id, run_id=run_id)
                return run_id

            run_id = self.queue.transaction(begin)
            cancel_event = threading.Event()
            self._running[run_id] = (agent, cancel_event)
        prompt = self._prompt(thread_id, task)
        try:
            result = agent.run(prompt, agent.work_dir, cancel_event=cancel_event)
        except BaseException as exc:
            from .agents import AgentResult
            result = AgentResult("", False, str(exc), time.time() - started, None)
        with self._lock:
            self._running.pop(run_id, None)
            killed = self._run_status(run_id) == "killing"
            status = "killed" if killed else ("ok" if result.ok else "error")
            def finish():
                self.queue.db.execute(
                    "UPDATE agent_runs SET status=?,ended=?,ok=?,error=?,duration=? WHERE id=?",
                    (status, time.time(), int(result.ok and not killed), result.error, result.duration, run_id),
                )
                self.queue.audit(
                    "agent_run_end", actor=agent.name, thread_id=thread_id, run_id=run_id,
                    detail={"status": status, "ok": bool(result.ok and not killed), "error": result.error, "duration": result.duration},
                )
            self.queue.transaction(finish)
        if killed:
            return []
        reply = self._append_message(thread_id, "agent", agent.name, result.output or result.error or "")
        if result.ok:
            self._gate_action_requests(thread_id, agent.name, result.output)
        replies = [reply]
        if result.ok:
            for mention in MENTION_RE.finditer(result.output):
                if mention.group("kind").lower() == "task":
                    replies.extend(self._wake(
                        thread_id, mention.group("agent"), mention.group("body"),
                        "agent", agent.name,
                    ))
        return replies

    def _prompt(self, thread_id: str, task: str) -> str:
        lines = []
        for message in self.messages(thread_id):
            role = "user" if message["author_type"] == "user" else f"agent:{message['author']}"
            body = self._escape_multiline(message["body"])
            lines.append(f"[{role}] {message['author']}: {body}")
        transcript = "\n".join(lines)
        policy = (
            "Do not push, merge, send messages, or make outbound network requests. "
            "To request one of those actions, emit exactly one line in this form: "
            'ACTION_REQUEST: {"action":"push|merge|send|network","summary":"..."}. '
            "The queue will create a human approval ticket; do not perform the action."
        )
        escaped_task = self._escape_multiline(task, indent_first=True)
        return f"Safety policy:\n{policy}\n\nThread transcript:\n{transcript}\n\nCurrent task:\n{escaped_task}"

    @staticmethod
    def _escape_multiline(text: str, *, indent_first: bool = False) -> str:
        """Normalize every Unicode line boundary and quote non-record lines."""
        lines = text.splitlines() or [""]
        escaped = "\n  ".join(lines)
        return f"  {escaped}" if indent_first else escaped

    def _gate_action_requests(self, thread_id: str, agent: str, output: str) -> None:
        for match in ACTION_RE.finditer(output):
            try:
                request, _ = json.JSONDecoder().raw_decode(output, match.end())
            except json.JSONDecodeError as exc:
                self.queue.audit(
                    "action_request_parse_error", actor=agent, thread_id=thread_id,
                    detail={"error": str(exc)},
                )
                continue
            action = request.get("action") if isinstance(request, dict) else None
            if action not in OUTBOUND_ACTIONS or not isinstance(request.get("summary"), str):
                self.queue.audit("action_request_parse_error", actor=agent, thread_id=thread_id,
                                 detail={"error": "invalid action or summary"})
                continue
            request["summary"] = self._sanitize_summary(request["summary"])
            self._create_approval_ticket(
                thread_id, agent, "action_request",
                f"Approve {action}: {request['summary']}", request,
            )

    def pause(self, agent: str | None = None) -> None:
        self._set_control(agent, "paused", "pause")

    def resume(self, agent: str | None = None) -> None:
        self._set_control(agent, "active", "resume")

    def kill(self, agent: str | None = None) -> None:
        self._set_control(agent, "killed", "kill")
        key = agent.casefold() if agent else None
        with self._lock:
            targets = [(run_id, runner, event) for run_id, (runner, event) in self._running.items()
                       if key is None or runner.name.casefold() == key]
            for run_id, runner, event in targets:
                self.queue.db.execute("UPDATE agent_runs SET status='killing' WHERE id=?", (run_id,))
                runner.cancel(event)

    def _set_control(self, agent: str | None, state: str, event: str) -> None:
        def change():
            if agent is None:
                self.queue.db.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('all_agents_state',?)", (state,)
                )
            else:
                self.queue.db.execute(
                    "INSERT OR REPLACE INTO agent_controls(agent,state) VALUES(?,?)",
                    (agent.casefold(), state),
                )
            self.queue.audit(event, actor="human", detail={"agent": agent or "all"})
        self.queue.transaction(change)

    def _control_state(self, agent: str) -> str:
        row = self.queue.db.execute("SELECT value FROM meta WHERE key='all_agents_state'").fetchone()
        if row and row["value"] != "active":
            return row["value"]
        row = self.queue.db.execute("SELECT state FROM agent_controls WHERE agent=?", (agent,)).fetchone()
        return row["state"] if row else "active"

    def _run_status(self, run_id: int) -> str:
        return self.queue.db.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()["status"]

    def _validate_work_dir(self, agent: Agent) -> None:
        work_dir = agent.work_dir
        if work_dir is None:
            raise ValueError(f"agent {agent.name!r} requires an explicit work_dir")
        if not work_dir.is_dir():
            raise ValueError(f"agent {agent.name!r} work_dir is not a directory: {work_dir}")
        if not any(work_dir.is_relative_to(folder.resolve()) for folder in agent.limits.allowed_folders):
            raise ValueError(f"agent {agent.name!r} work_dir must be inside allowed_folders")
        state = self.queue.state
        if state.is_relative_to(work_dir) or work_dir.is_relative_to(state):
            raise ValueError(f"agent {agent.name!r} work_dir must not overlap the state directory")

    @staticmethod
    def _sanitize_summary(summary: str) -> str:
        summary = re.sub(r"[\x00-\x1f\x7f]+", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()
        summary = re.sub(r"(?i)\b(APPROVE|SKIP)(?=\s+TKT-)", r"\1_", summary)
        return summary[:SUMMARY_LIMIT] or "(no summary)"

    def _create_approval_ticket(
        self, thread_id: str, agent: str, request_type: str, title: str, request: dict
    ) -> dict:
        clean_title = self._sanitize_summary(title)

        def create():
            ticket_id = self.queue._next_id()
            stamp = time.time()
            payload = {
                "reason": f"Agent request from {agent} in {thread_id}",
                "artifacts": {},
                "notes": "",
                request_type: request,
            }
            self.queue.db.execute(
                "INSERT INTO tickets(id,kind,status,title,company,url,owner,payload,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ticket_id, "task", "review", clean_title, "", None, agent,
                 json.dumps(payload, sort_keys=True), stamp, stamp),
            )
            self.queue._event(ticket_id, "new", agent, payload["reason"])
            self.queue._event(ticket_id, "drafting", agent, "claimed by agent")
            self.queue._event(ticket_id, "review", agent, "requires human approval")
            self.queue.audit(
                "approval_requested", actor=agent, thread_id=thread_id,
                ticket_id=ticket_id, detail={**request, "type": request_type},
            )
            return self.queue.get(ticket_id)

        ticket = self.queue.transaction(create)
        self.queue._notify(
            f"{ticket['id']} ready\n - {ticket['title']}\n"
            f"Reply:\nAPPROVE {ticket['id']} CODEX\n"
            f"APPROVE {ticket['id']} CLAUDE\nSKIP {ticket['id']}",
            source="hitl-review",
        )
        return ticket

    def dispatch_approved_agent_task(self, ticket: dict) -> list[dict]:
        request = ticket["payload"]["agent_task"]
        thread_id = next(
            row["thread_id"] for row in self.audit_log()
            if row["ticket_id"] == ticket["id"] and row["event"] == "approval_requested"
        )
        return self._wake(
            thread_id, request["target_agent"], request["task"], "user", "human-approval"
        )
