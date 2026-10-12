"""Local, durable agent usage accounting and quota-aware routing."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, TYPE_CHECKING

from .agents.base import Agent, UsagePlan

if TYPE_CHECKING:
    from .queue import Queue


TOKEN_FIELDS = ("total_tokens", "tokens", "token_count")
INPUT_FIELDS = ("input_tokens", "prompt_tokens")
OUTPUT_FIELDS = ("output_tokens", "completion_tokens")


def token_count(usage: Mapping | None) -> int | None:
    """Extract common OpenAI-compatible token fields without inventing a value."""
    if not isinstance(usage, Mapping):
        return None
    for key in TOKEN_FIELDS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    parts = []
    for fields in (INPUT_FIELDS, OUTPUT_FIELDS):
        value = next((usage.get(key) for key in fields if key in usage), None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            parts.append(value)
    return sum(parts) if parts else None


class UsageMonitor:
    def __init__(self, queue: Queue, *, clock: Callable[[], float] = time.time) -> None:
        self.queue = queue
        self.clock = clock
        self._agents: dict[str, Agent] = {}
        self.queue.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_runs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER UNIQUE,
              agent TEXT NOT NULL,
              started REAL NOT NULL,
              duration REAL NOT NULL,
              ok INTEGER NOT NULL,
              tokens INTEGER,
              usage_json TEXT
            );
            CREATE INDEX IF NOT EXISTS usage_runs_agent_started
              ON usage_runs(agent, started);
            CREATE TABLE IF NOT EXISTS usage_alerts(
              agent TEXT NOT NULL,
              window_key TEXT NOT NULL,
              threshold TEXT NOT NULL,
              at REAL NOT NULL,
              PRIMARY KEY(agent, window_key, threshold)
            );
            """
        )

    def configure(self, agents: Mapping[str, Agent]) -> None:
        self._agents = {name.casefold(): agent for name, agent in agents.items()}

    def begin(self, agent_key: str, *, run_id: int, started: float) -> None:
        """Reserve a run against quota in the caller's start transaction."""
        self.queue.db.execute(
            "INSERT INTO usage_runs(run_id,agent,started,duration,ok,tokens,usage_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, agent_key.casefold(), started, 0.0, 0, None, None),
        )

    def record(self, agent_key: str, *, run_id: int, started: float, duration: float,
               ok: bool, usage: Mapping | None) -> dict:
        agent = self._agent(agent_key)
        tokens = token_count(usage)
        serialized = json.dumps(dict(usage), sort_keys=True) if isinstance(usage, Mapping) else None
        self.queue.transaction(lambda: self.queue.db.execute(
            "INSERT INTO usage_runs(run_id,agent,started,duration,ok,tokens,usage_json) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "agent=excluded.agent,started=excluded.started,duration=excluded.duration,"
            "ok=excluded.ok,tokens=excluded.tokens,usage_json=excluded.usage_json",
            (run_id, agent_key.casefold(), started, max(0.0, duration), int(ok), tokens, serialized),
        ))
        result = self.summary(agent_key)
        self._check_thresholds(agent_key.casefold(), agent, result)
        return result

    def _agent(self, agent_key: str) -> Agent:
        agent = self._agents.get(agent_key.casefold())
        if agent is None:
            raise ValueError(f"Unknown agent {agent_key}")
        return agent

    def summary(self, agent_name: str | None = None) -> dict | list[dict]:
        if agent_name is not None:
            key = agent_name.casefold()
            return self._summary(key, self._agent(key))
        return [self._summary(key, agent) for key, agent in sorted(self._agents.items())]

    def can_run(self, agent_name: str) -> bool:
        item = self.summary(agent_name)
        return not item["blocked"]

    def route(self, task: str, eligible_agents: Iterable[str],
              *, control_state: Callable[[str], str] | None = None) -> dict:
        candidates = []
        skipped = {}
        for requested in eligible_agents:
            key = requested.casefold()
            agent = self._agents.get(key)
            if agent is None:
                skipped[requested] = "unknown"
                continue
            state = control_state(key) if control_state else "active"
            item = self._summary(key, agent)
            if state != "active":
                skipped[agent.name] = state
            elif item["blocked"]:
                skipped[agent.name] = "quota blocked"
            else:
                candidates.append((item["remaining_fraction"], agent.name.casefold(), agent, item))
        if not candidates:
            return {"agent": None, "task": task, "skipped": skipped,
                    "explanation": "No eligible active agent has remaining quota."}
        remaining = max(row[0] for row in candidates)
        best = min((row for row in candidates if row[0] == remaining), key=lambda row: row[1])
        remaining, _, agent, item = best
        return {
            "agent": agent.name,
            "task": task,
            "remaining_fraction": remaining,
            "skipped": skipped,
            "explanation": (
                f"Selected {agent.name}: {remaining * 100:.1f}% quota headroom remains; "
                "ties are resolved by agent name."
            ),
            "usage": item,
        }

    def _summary(self, agent_key: str, agent: Agent) -> dict:
        plan = agent.usage_plan
        if plan is None:
            return {"agent": agent.name, "window": None, "window_start": None,
                    "used": {"runs": 0, "minutes": 0.0, "tokens": None},
                    "caps": {}, "percent_used": {}, "remaining_fraction": 1.0,
                    "blocked": False}
        now = self.clock()
        start, key = _window(plan.window, now)
        row = self.queue.db.execute(
            "SELECT COUNT(*) runs,COALESCE(SUM(duration),0) seconds,SUM(tokens) tokens "
            "FROM usage_runs WHERE agent=? COLLATE NOCASE AND started>=? AND started<=?",
            (agent_key, start, now),
        ).fetchone()
        used = {"runs": row["runs"], "minutes": row["seconds"] / 60.0, "tokens": row["tokens"]}
        caps = {key: value for key, value in {
            "runs": plan.runs, "minutes": plan.minutes, "tokens": plan.tokens
        }.items() if value is not None}
        percents = {
            metric: (used[metric] / cap * 100.0 if used[metric] is not None else None)
            for metric, cap in caps.items()
        }
        known = [value / 100.0 for value in percents.values() if value is not None]
        fraction = max(known, default=0.0)
        return {"agent": agent.name, "window": plan.window, "window_start": start,
                "window_key": key, "used": used, "caps": caps, "percent_used": percents,
                "remaining_fraction": max(0.0, 1.0 - fraction),
                "blocked": fraction >= plan.blocked_threshold}

    def _check_thresholds(self, agent_key: str, agent: Agent, item: dict) -> None:
        plan = agent.usage_plan
        if plan is None:
            return
        values = [value for value in item["percent_used"].values() if value is not None]
        fraction = max(values, default=0.0) / 100.0
        if plan.window.endswith("h"):
            self.queue.transaction(lambda: self.queue.db.execute(
                "DELETE FROM usage_alerts WHERE agent=? AND window_key=? AND at<?",
                (agent_key, item["window_key"], item["window_start"]),
            ))
        levels = (("warn", plan.warn_threshold), ("blocked", plan.blocked_threshold))
        for level, threshold in levels:
            if fraction < threshold:
                continue
            detail = {"level": level, "window": plan.window,
                      "percent_used": item["percent_used"], "caps": item["caps"]}
            def insert_and_audit():
                inserted = self.queue.db.execute(
                    "INSERT OR IGNORE INTO usage_alerts(agent,window_key,threshold,at) VALUES(?,?,?,?)",
                    (agent_key, item["window_key"], level, self.clock()),
                ).rowcount
                if inserted:
                    self.queue.audit(f"usage_{level}", actor=agent.name, detail=detail)
                return inserted

            inserted = self.queue.transaction(insert_and_audit)
            if not inserted:
                continue
            self.queue._notify(
                f"Usage {level}: {agent.name} reached {fraction * 100:.1f}% of its {plan.window} plan",
                source="hitl-usage",
            )


def _window(spec: str, now: float) -> tuple[float, str]:
    current = datetime.fromtimestamp(now, timezone.utc)
    if spec == "daily":
        start = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return start, f"daily:{current.date().isoformat()}"
    if spec == "weekly":
        midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight.timestamp() - current.weekday() * 86400
        return start, f"weekly:{datetime.fromtimestamp(start, timezone.utc).date().isoformat()}"
    seconds = float(spec[:-1]) * 3600
    return now - seconds, f"rolling:{spec}"
