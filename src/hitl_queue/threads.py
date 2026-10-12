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
ACTION_RE = re.compile(r"(?m)^ACTION_REQUEST:\s*(\{.*\})\s*$")
OUTBOUND_ACTIONS = {"push", "merge", "send", "network"}


class Threads:
    """Thread API. ``post`` synchronously runs agents named by task mentions."""

    def __init__(self, queue: Queue):
        self.queue = queue
        self._agents: dict[str, Agent] = {}
        self._running: dict[int, Agent] = {}
        self._lock = threading.RLock()
        self.queue.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads(
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              max_agent_runs INTEGER NOT NULL CHECK(max_agent_runs > 0),
              agent_runs INTEGER NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'active',
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
            """
        )

    def configure(self, agents: dict[str, Agent]) -> None:
        self._agents = {name.casefold(): agent for name, agent in agents.items()}

    def configure_from(self, path: str | Path = "agents.toml") -> None:
        """Load adapters using the public ``hitl_queue.agents`` configuration."""
        from .agents import load_agents

        self.configure(load_agents(path))

    def create(self, title: str, *, max_agent_runs: int = 12) -> dict:
        if not title.strip():
            raise ValueError("thread title required")
        if not isinstance(max_agent_runs, int) or max_agent_runs < 1:
            raise ValueError("max_agent_runs must be a positive integer")

        def insert():
            row = self.queue.db.execute("SELECT value FROM meta WHERE key='next_thread_seq'").fetchone()
            number = int(row["value"]) if row else 1
            self.queue.db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('next_thread_seq',?)",
                (str(number + 1),),
            )
            thread_id = f"THR-{number:04d}"
            self.queue.db.execute(
                "INSERT INTO threads(id,title,max_agent_runs,created) VALUES(?,?,?,?)",
                (thread_id, title.strip(), max_agent_runs, time.time()),
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
        thread = self.get(thread_id)
        if thread["state"] != "active":
            raise ValueError(f"{thread_id} is {thread['state']}")
        message = self._append_message(thread_id, "user", author, body)
        replies = []
        for mention in MENTION_RE.finditer(body):
            if mention.group("kind").lower() == "fyi":
                continue
            replies.extend(self._wake(
                thread_id, mention.group("agent"), mention.group("body"), cwd
            ))
        return {"message": message, "replies": replies}

    def _append_message(self, thread_id: str, author_type: str, author: str, body: str) -> dict:
        if not body.strip():
            raise ValueError("message body required")
        cursor = self.queue.db.execute(
            "INSERT INTO thread_messages(thread_id,author_type,author,body,created) VALUES(?,?,?,?,?)",
            (thread_id, author_type, author, body, time.time()),
        )
        message_id = cursor.lastrowid
        self.queue.audit(
            "message", actor=author, thread_id=thread_id, message_id=message_id,
            detail={"author_type": author_type, "body": body},
        )
        return dict(self.queue.db.execute("SELECT * FROM thread_messages WHERE id=?", (message_id,)).fetchone())

    def _wake(self, thread_id: str, name: str, task: str, cwd: str | Path | None) -> list[dict]:
        key = name.casefold()
        agent = self._agents.get(key)
        if agent is None:
            raise ValueError(f"Unknown agent {name}")
        with self._lock:
            thread = self.get(thread_id)
            if thread["state"] != "active" or self._control_state(key) != "active":
                return []
            if thread["agent_runs"] >= thread["max_agent_runs"]:
                raise ValueError(f"{thread_id} agent run limit reached")
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
            self._running[run_id] = agent
        prompt = self._prompt(thread_id, task)
        try:
            result = agent.run(prompt, cwd or self.queue.state)
        except BaseException as exc:
            from .agents import AgentResult
            result = AgentResult("", False, str(exc), time.time() - started, None)
        with self._lock:
            self._running.pop(run_id, None)
            killed = self._run_status(run_id) == "killing"
            status = "killed" if killed else ("ok" if result.ok else "error")
            self.queue.db.execute(
                "UPDATE agent_runs SET status=?,ended=?,ok=?,error=?,duration=? WHERE id=?",
                (status, time.time(), int(result.ok and not killed), result.error, result.duration, run_id),
            )
            self.queue.audit(
                "agent_run_end", actor=agent.name, thread_id=thread_id, run_id=run_id,
                detail={"status": status, "ok": bool(result.ok and not killed), "error": result.error, "duration": result.duration},
            )
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
                        thread_id, mention.group("agent"), mention.group("body"), cwd
                    ))
        return replies

    def _prompt(self, thread_id: str, task: str) -> str:
        transcript = "\n".join(f"{m['author']}: {m['body']}" for m in self.messages(thread_id))
        policy = (
            "Do not push, merge, send messages, or make outbound network requests. "
            "To request one of those actions, emit exactly one line in this form: "
            'ACTION_REQUEST: {"action":"push|merge|send|network","summary":"..."}. '
            "The queue will create a human approval ticket; do not perform the action."
        )
        return f"Safety policy:\n{policy}\n\nThread transcript:\n{transcript}\n\nCurrent task:\n{task}"

    def _gate_action_requests(self, thread_id: str, agent: str, output: str) -> None:
        for match in ACTION_RE.finditer(output):
            try:
                request = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid ACTION_REQUEST JSON: {exc}") from exc
            action = request.get("action")
            if action not in OUTBOUND_ACTIONS or not isinstance(request.get("summary"), str):
                raise ValueError("ACTION_REQUEST needs action push|merge|send|network and summary")
            ticket = self.queue.enqueue(
                title=f"Approve {action}: {request['summary']}", kind="task",
                reason=f"Agent action request from {agent} in {thread_id}", actor=agent,
            )
            self.queue.claim(ticket["id"], agent)
            reviewed = self.queue.submit_draft(
                ticket["id"], actor=agent, note="outbound action requires human approval"
            )
            payload = reviewed["payload"]
            payload["action_request"] = request
            self.queue.db.execute(
                "UPDATE tickets SET payload=?,updated=? WHERE id=?",
                (json.dumps(payload, sort_keys=True), time.time(), ticket["id"]),
            )
            self.queue.audit(
                "approval_requested", actor=agent, thread_id=thread_id,
                ticket_id=ticket["id"], detail=request,
            )

    def pause(self, agent: str | None = None) -> None:
        self._set_control(agent, "paused")

    def resume(self, agent: str | None = None) -> None:
        self._set_control(agent, "active")

    def kill(self, agent: str | None = None) -> None:
        self._set_control(agent, "killed")
        key = agent.casefold() if agent else None
        with self._lock:
            targets = [(run_id, runner) for run_id, runner in self._running.items()
                       if key is None or runner.name.casefold() == key]
            for run_id, runner in targets:
                self.queue.db.execute("UPDATE agent_runs SET status='killing' WHERE id=?", (run_id,))
                runner.cancel()

    def _set_control(self, agent: str | None, state: str) -> None:
        if agent is None:
            self.queue.db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('all_agents_state',?)", (state,)
            )
        else:
            self.queue.db.execute(
                "INSERT OR REPLACE INTO agent_controls(agent,state) VALUES(?,?)",
                (agent.casefold(), state),
            )

    def _control_state(self, agent: str) -> str:
        row = self.queue.db.execute("SELECT value FROM meta WHERE key='all_agents_state'").fetchone()
        if row and row["value"] != "active":
            return row["value"]
        row = self.queue.db.execute("SELECT state FROM agent_controls WHERE agent=?", (agent,)).fetchone()
        return row["state"] if row else "active"

    def _run_status(self, run_id: int) -> str:
        return self.queue.db.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()["status"]
