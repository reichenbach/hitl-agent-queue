"""Durable agent/user conversations with bounded, auditable execution."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .agents import Agent
from .text import sanitize_summary
from .usage import UsageNotifyError

if TYPE_CHECKING:
    from .queue import Queue


MENTION_RE = re.compile(
    r"(?im)(?:^|\s)@(?P<agent>[A-Za-z][\w.-]*)\s+(?P<kind>task|fyi)\s*:\s*(?P<body>[^\n]*)"
)
ACTION_RE = re.compile(r"(?i)(?<![\w])ACTION_REQUEST\s*:\s*")
REVIEW_VERDICT_RE = re.compile(r"(?im)^\s*(?:REVIEW_)?VERDICT\s*:\s*(.*)$")
OUTBOUND_ACTIONS = {"push", "merge", "send", "network"}
SUMMARY_LIMIT = 160
logger = logging.getLogger(__name__)


class Threads:
    """Thread API. ``post`` synchronously runs agents named by task mentions."""

    def __init__(self, queue: Queue):
        self.queue = queue
        self._agents: dict[str, Agent] = {}
        self._running: dict[int, tuple[str, Agent, threading.Event]] = {}
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
            CREATE TABLE IF NOT EXISTS review_rounds(
              thread_id TEXT NOT NULL REFERENCES threads(id),
              scope TEXT NOT NULL,
              change_count INTEGER NOT NULL DEFAULT 0,
              blocked INTEGER NOT NULL DEFAULT 0,
              approval_ticket_id TEXT,
              PRIMARY KEY(thread_id, scope)
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
        self._set_max_review_rounds(self.queue.cfg.get("max_review_rounds", 3))

    def configure(self, agents: dict[str, Agent]) -> None:
        for agent in agents.values():
            self._validate_work_dir(agent)
        self._agents = {name.casefold(): agent for name, agent in agents.items()}
        self.queue.usage.configure(agents)

    def configure_from(self, path: str | Path = "agents.toml") -> None:
        """Load adapters using the public ``hitl_queue.agents`` configuration."""
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            import tomli as tomllib
        from .agents import load_agents

        with Path(path).expanduser().open("rb") as handle:
            document = tomllib.load(handle)
        threads_config = document.get("threads") or {}
        if "max_review_rounds" in threads_config:
            self._set_max_review_rounds(threads_config["max_review_rounds"])
        self.configure(load_agents(path))

    def _set_max_review_rounds(self, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("max_review_rounds must be a positive integer")
        self.queue.cfg["max_review_rounds"] = value

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
        self,
        thread_id: str,
        name: str,
        task: str,
        source_type: str,
        source_name: str,
        *,
        run_started: list[bool] | None = None,
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
            blocked = self._blocked_review_scope(thread_id)
            if blocked:
                self._hold_review_handoff(
                    thread_id, blocked, source_name, agent.name, task
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
            if not self.queue.usage.can_run(key):
                return []
            if thread["agent_runs"] >= thread["max_agent_runs"]:
                self.queue.audit(
                    "agent_run_limit", actor=source_name, thread_id=thread_id,
                    detail={"limit": thread["max_agent_runs"], "target": agent.name},
                )
                return []
            started = self.queue.usage.clock()

            def begin():
                self.queue.db.execute(
                    "UPDATE threads SET agent_runs=agent_runs+1 WHERE id=?", (thread_id,)
                )
                cursor = self.queue.db.execute(
                    "INSERT INTO agent_runs(thread_id,agent,status,started) VALUES(?,?,?,?)",
                    (thread_id, agent.name, "running", started),
                )
                run_id = cursor.lastrowid
                self.queue.usage.begin(key, run_id=run_id, started=started)
                self.queue.audit("agent_run_start", actor=agent.name, thread_id=thread_id, run_id=run_id)
                return run_id

            run_id = self.queue.transaction(begin)
            if run_started is not None:
                run_started.append(True)
            cancel_event = threading.Event()
            self._running[run_id] = (key, agent, cancel_event)

        result = None
        killed = False
        finished = False

        def finish(status: str, ok: bool, error: str | None, duration: float):
            def update():
                self.queue.db.execute(
                    "UPDATE agent_runs SET status=?,ended=?,ok=?,error=?,duration=? WHERE id=?",
                    (status, time.time(), int(ok), error, duration, run_id),
                )
                self.queue.audit(
                    "agent_run_end", actor=agent.name, thread_id=thread_id, run_id=run_id,
                    detail={"status": status, "ok": ok, "error": error, "duration": duration},
                )
            self.queue.transaction(update)

        try:
            prompt = self._prompt(thread_id, task)
            try:
                result = agent.run(prompt, agent.work_dir, cancel_event=cancel_event)
            except BaseException as exc:
                from .agents import AgentResult
                result = AgentResult("", False, str(exc), self.queue.usage.clock() - started, None)
            with self._lock:
                killed = self._run_status(run_id) == "killing"
                status = "killed" if killed else ("ok" if result.ok else "error")
                finish(
                    status, bool(result.ok and not killed), result.error, result.duration
                )
                self._running.pop(run_id, None)
                finished = True
        except BaseException as exc:
            duration = result.duration if result is not None else self.queue.usage.clock() - started
            finish("error", False, str(exc), duration)
            self._record_usage(
                key, agent, thread_id=thread_id, run_id=run_id, started=started, duration=duration,
                ok=False, usage=result.usage if result is not None else None,
            )
            raise
        finally:
            if not finished:
                with self._lock:
                    self._running.pop(run_id, None)
        self._record_usage(
            key, agent, thread_id=thread_id, run_id=run_id, started=started, duration=result.duration,
            ok=bool(result.ok and not killed), usage=result.usage,
        )
        if killed:
            return []
        reply = self._append_message(thread_id, "agent", agent.name, result.output or result.error or "")
        if result.ok:
            self._gate_action_requests(thread_id, agent.name, result.output)
        review = self._record_review_verdict(thread_id, agent.name, result.output) if result.ok else None
        replies = [reply]
        if result.ok:
            for mention in MENTION_RE.finditer(result.output):
                if mention.group("kind").lower() == "task":
                    target = mention.group("agent")
                    if target.casefold() not in self._agents:
                        self.queue.audit(
                            "unknown_agent_mention", actor=agent.name, thread_id=thread_id,
                            detail={"target": target, "kind": "task"},
                        )
                        continue
                    replies.extend(self._wake(
                        thread_id, target, mention.group("body"), "agent", agent.name,
                    ))
        return replies

    def _record_review_verdict(self, thread_id: str, reviewer: str, output: str) -> dict | None:
        """Persist a CHANGE verdict and return its durable scope state."""
        match = REVIEW_VERDICT_RE.search(output)
        if not match:
            return None
        raw = match.group(1).strip()
        data: dict = {}
        if raw.startswith("{"):
            try:
                parsed, _ = json.JSONDecoder().raw_decode(raw)
                data = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                self.queue.audit("review_verdict_parse_error", actor=reviewer,
                                 thread_id=thread_id, detail={"error": "invalid JSON"})
                return None
        else:
            data["verdict"] = raw.split()[0] if raw else ""
        if str(data.get("verdict", "")).upper() != "CHANGE":
            return None
        raw_branch = str(data.get("branch", "")).strip()
        branch = self._sanitize_summary(raw_branch) if raw_branch else ""
        scope = self._review_scope(thread_id)
        summary = self._sanitize_summary(str(data.get("summary") or output))
        findings = data.get("findings", [])
        if not isinstance(findings, list):
            findings = [str(findings)]
        findings = [self._sanitize_summary(str(item)) for item in findings[:10] if str(item).strip()]
        prior = next((m for m in reversed(self.messages(thread_id)[:-1])
                      if m["author_type"] == "agent" and m["author"] != reviewer), None)
        builder_summary = self._sanitize_summary(prior["body"]) if prior else "No builder summary available"
        limit = int(self.queue.cfg.get("max_review_rounds", 3))

        def record():
            self.queue.db.execute(
                "INSERT INTO review_rounds(thread_id,scope,change_count) VALUES(?,?,1) "
                "ON CONFLICT(thread_id,scope) DO UPDATE SET change_count=change_count+1",
                (thread_id, scope),
            )
            row = self.queue.db.execute(
                "SELECT * FROM review_rounds WHERE thread_id=? AND scope=?", (thread_id, scope)
            ).fetchone()
            blocked = bool(row["blocked"] or row["change_count"] >= limit)
            if blocked and not row["blocked"]:
                self.queue.db.execute(
                    "UPDATE review_rounds SET blocked=1 WHERE thread_id=? AND scope=?",
                    (thread_id, scope),
                )
            self.queue.audit(
                "review_change", actor=reviewer, thread_id=thread_id,
                detail={"scope": scope, "round": row["change_count"], "limit": limit,
                        "blocked": blocked, "summary": summary, "findings": findings},
            )
            return {"scope": scope, "round": row["change_count"], "limit": limit,
                    "blocked": blocked, "reviewer_summary": summary,
                    "builder_summary": builder_summary, "findings": findings,
                    "branch": branch,
                    "newly_blocked": blocked and not bool(row["blocked"])}

        def record_and_escalate():
            review = record()
            ticket = None
            if review["newly_blocked"]:
                ticket = self._insert_approval_ticket(
                    thread_id, reviewer, "review_round_cap",
                    f"Review round cap reached for {review['scope']}", review,
                )
                self.queue.db.execute(
                    "UPDATE review_rounds SET approval_ticket_id=? "
                    "WHERE thread_id=? AND scope=? AND approval_ticket_id IS NULL",
                    (ticket["id"], thread_id, review["scope"]),
                )
                self.queue.audit("review_round_cap", actor=reviewer, thread_id=thread_id,
                                 ticket_id=ticket["id"], detail=review)
            return review, ticket

        review, ticket = self.queue.transaction(record_and_escalate)
        if ticket:
            self._notify_approval_ticket(ticket)
        return review

    def _review_scope(self, thread_id: str) -> str:
        """Use the latest non-escalation ticket named by a human."""
        escalation_ids = {
            row["approval_ticket_id"].upper()
            for row in self.queue.db.execute(
                "SELECT approval_ticket_id FROM review_rounds "
                "WHERE thread_id=? AND approval_ticket_id IS NOT NULL",
                (thread_id,),
            )
        }
        for message in reversed(self.messages(thread_id)):
            if message["author_type"] != "user":
                continue
            matches = re.findall(r"(?i)(?<![\w-])(TKT-\d+)(?![\w-])", message["body"])
            for ticket_id in reversed(matches):
                ticket_id = ticket_id.upper()
                if ticket_id not in escalation_ids:
                    return f"ticket:{ticket_id}"
        return f"thread:{thread_id}"

    def _blocked_review_scope(self, thread_id: str) -> dict | None:
        row = self.queue.db.execute(
            "SELECT * FROM review_rounds WHERE thread_id=? AND blocked=1 "
            "ORDER BY scope LIMIT 1",
            (thread_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "scope": row["scope"], "round": row["change_count"],
            "limit": int(self.queue.cfg.get("max_review_rounds", 3)), "blocked": True,
            "reviewer_summary": "Review scope remains blocked",
            "builder_summary": "Awaiting human approval", "findings": [],
        }

    def _hold_review_handoff(
        self, thread_id: str, review: dict, reviewer: str, target: str, task: str
    ) -> None:
        def hold():
            row = self.queue.db.execute(
                "SELECT approval_ticket_id FROM review_rounds WHERE thread_id=? AND scope=?",
                (thread_id, review["scope"]),
            ).fetchone()
            if not row or not row["approval_ticket_id"]:
                return None
            ticket = self.queue.get(row["approval_ticket_id"])
            if ticket["status"] not in {"review", "approved"}:
                return None
            payload = ticket["payload"]
            payload["review_round_cap"].update({"target_agent": target, "task": task})
            self.queue.db.execute(
                "UPDATE tickets SET payload=?,updated=? WHERE id=? "
                "AND status IN ('review','approved')",
                (json.dumps(payload, sort_keys=True), time.time(), ticket["id"]),
            )
            self.queue.audit("review_handoff_blocked", actor=reviewer, thread_id=thread_id,
                             ticket_id=ticket["id"],
                             detail={"scope": review["scope"], "target": target})
            return ticket
        self.queue.transaction(hold)

    def dispatch_review_round_cap(self, ticket: dict) -> tuple[list[dict], bool]:
        request = ticket["payload"]["review_round_cap"]
        thread_id, scope = request["thread_id"], request["scope"]
        self.queue.transaction(lambda: self._reset_review_scope(thread_id, scope, ticket["id"]))
        if not request.get("target_agent"):
            return [], True
        started: list[bool] = []
        try:
            replies = self._wake(thread_id, request["target_agent"], request["task"],
                                 "user", "human-approval", run_started=started)
        except Exception as exc:
            if not started:
                self.queue.transaction(lambda: self._restore_review_scope(
                    thread_id, scope, ticket["id"], int(request["round"])
                ))
                raise
            self.queue.audit(
                "review_round_dispatch_error", actor="human", ticket_id=ticket.get("id"),
                thread_id=thread_id, detail={"error": str(exc)},
            )
            return [], True
        if not started:
            self.queue.transaction(lambda: self._restore_review_scope(
                thread_id, scope, ticket["id"], int(request["round"])
            ))
        return replies, bool(started)

    def _reset_review_scope(self, thread_id: str, scope: str, ticket_id: str) -> None:
        self.queue.db.execute(
            "UPDATE review_rounds SET change_count=0,blocked=0,approval_ticket_id=NULL "
            "WHERE thread_id=? AND scope=?", (thread_id, scope),
        )
        self.queue.audit("review_round_reset", actor="human", thread_id=thread_id,
                         ticket_id=ticket_id, detail={"scope": scope})

    def _restore_review_scope(
        self, thread_id: str, scope: str, ticket_id: str, count: int
    ) -> None:
        self.queue.db.execute(
            "UPDATE review_rounds SET change_count=?,blocked=1,approval_ticket_id=? "
            "WHERE thread_id=? AND scope=? "
            "AND (approval_ticket_id IS NULL OR approval_ticket_id=?)",
            (count, ticket_id, thread_id, scope, ticket_id),
        )
        self.queue.audit("review_round_reset_rolled_back", actor="system",
                         thread_id=thread_id, ticket_id=ticket_id,
                         detail={"scope": scope, "count": count})

    def _record_usage(self, key: str, agent: Agent, *, thread_id: str, **fields) -> None:
        """Accounting must never change the observable outcome of an agent run."""
        try:
            self.queue.usage.record(key, **fields)
        except UsageNotifyError as exc:
            logger.exception("failed to notify usage for run %s", fields["run_id"])
            try:
                self.queue.transaction(lambda: self.queue.audit(
                    "usage_notify_error", actor=agent.name, thread_id=thread_id,
                    run_id=fields["run_id"], detail={"error": str(exc)},
                ))
            except BaseException:
                logger.exception("failed to audit usage notification failure for run %s",
                                 fields["run_id"])
        except BaseException as exc:
            logger.exception("failed to record usage for run %s", fields["run_id"])
            try:
                self.queue.transaction(lambda: self.queue.audit(
                    "usage_record_error", actor=agent.name, thread_id=thread_id,
                    run_id=fields["run_id"], detail={"error": str(exc)},
                ))
            except BaseException:
                logger.exception("failed to audit usage recording failure for run %s", fields["run_id"])

    def route(self, task: str, eligible_agents: list[str]) -> dict:
        """Choose the active eligible agent with the greatest quota headroom."""
        return self.queue.usage.route(task, eligible_agents, control_state=self._control_state)

    def running(self) -> list[dict]:
        """Return durable read-model data for agent runs that have not finished."""
        return [dict(row) for row in self.queue.db.execute(
            "SELECT id,thread_id,agent,status,started FROM agent_runs "
            "WHERE status IN ('running','killing') ORDER BY started,id"
        )]

    def _prompt(self, thread_id: str, task: str) -> str:
        lines = []
        for message in self.messages(thread_id):
            author = json.dumps(message["author"], ensure_ascii=False)[1:-1]
            role = "user" if message["author_type"] == "user" else f"agent:{author}"
            body = self._escape_multiline(message["body"])
            lines.append(f"[{role}] {author}: {body}")
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
            targets = [(run_id, runner, event)
                       for run_id, (config_key, runner, event) in self._running.items()
                       if key is None or config_key == key]
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
        if self._contains_directory(state, work_dir) or self._contains_directory(work_dir, state):
            raise ValueError(f"agent {agent.name!r} work_dir must not overlap the state directory")

    @staticmethod
    def _contains_directory(parent: Path, child: Path) -> bool:
        """Compare real directory identities, including every child ancestor."""
        parent_stat = parent.stat()
        identity = (parent_stat.st_dev, parent_stat.st_ino)
        return any(
            (candidate.stat().st_dev, candidate.stat().st_ino) == identity
            for candidate in (child, *child.parents)
        )

    @staticmethod
    def _sanitize_summary(summary: str) -> str:
        return sanitize_summary(summary, limit=SUMMARY_LIMIT)

    def _create_approval_ticket(
        self, thread_id: str, agent: str, request_type: str, title: str, request: dict
    ) -> dict:
        def create():
            return self._insert_approval_ticket(
                thread_id, agent, request_type, title, request
            )

        ticket = self.queue.transaction(create)
        self._notify_approval_ticket(ticket)
        return ticket

    def _insert_approval_ticket(
        self, thread_id: str, agent: str, request_type: str, title: str, request: dict
    ) -> dict:
        ticket_id = self.queue._next_id()
        stamp = time.time()
        payload = {
            "reason": f"Agent request from {agent} in {thread_id}", "artifacts": {}, "notes": "",
            request_type: {**request, "thread_id": thread_id},
        }
        self.queue.db.execute(
            "INSERT INTO tickets(id,kind,status,title,company,url,owner,payload,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, "task", "review", self._sanitize_summary(title), "", None, agent,
             json.dumps(payload, sort_keys=True), stamp, stamp),
        )
        self.queue._event(ticket_id, "new", agent, payload["reason"])
        self.queue._event(ticket_id, "drafting", agent, "claimed by agent")
        self.queue._event(ticket_id, "review", agent, "requires human approval")
        self.queue.audit("approval_requested", actor=agent, thread_id=thread_id,
                         ticket_id=ticket_id, detail={**request, "type": request_type})
        return self.queue.get(ticket_id)

    def _notify_approval_ticket(self, ticket: dict) -> None:
        self.queue._notify(
            f"{ticket['id']} ready\n - {ticket['title']}\n"
            f"Reply:\nAPPROVE {ticket['id']} CODEX\n"
            f"APPROVE {ticket['id']} CLAUDE\nSKIP {ticket['id']}",
            source="hitl-review",
        )

    def dispatch_approved_agent_task(self, ticket: dict) -> tuple[list[dict], bool]:
        request = ticket["payload"]["agent_task"]
        thread_id = request.get("thread_id")
        if not thread_id:
            raise ValueError("missing_thread_id")
        run_started: list[bool] = []
        replies: list[dict] = []
        try:
            replies = self._wake(
                thread_id,
                request["target_agent"],
                request["task"],
                "user",
                "human-approval",
                run_started=run_started,
            )
        except Exception as exc:
            if not run_started:
                raise
            self.queue.audit(
                "agent_task_dispatch_error", actor="human", ticket_id=ticket.get("id"),
                thread_id=thread_id, detail={"error": str(exc)},
            )
        return replies, bool(run_started)
