"""Shared human actions used by reply parsing and future app controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .queue import Queue


class Actions:
    def __init__(self, queue: Queue):
        self.queue = queue

    def approve(self, ticket_id: str, worker: str) -> dict:
        tid = ticket_id.upper()
        worker = worker.lower()
        if worker not in {"codex", "claude"}:
            raise ValueError("worker must be codex or claude")

        ticket = self.queue.get(tid)
        if ticket["status"] in {"new", "drafting"}:
            self.queue.set_preferred_worker(tid, worker)
            notification = self.queue._notify(
                f"Got it. {tid} → {worker} after draft. Reply SKIP {tid} to cancel.",
                source="hitl-reply",
            )
            return {
                "ok": True,
                "action": "prefer_worker",
                "id": tid,
                "worker": worker,
                "notification": notification,
            }

        agent_task = (ticket.get("payload") or {}).get("agent_task")
        review_cap = (ticket.get("payload") or {}).get("review_round_cap")
        if ticket["status"] in {"review", "approved"} and review_cap:
            if ticket["status"] == "review":
                ticket = self.queue.approve(tid, actor="human", note="continue review loop")
            try:
                replies, run_started = self.queue.threads.dispatch_review_round_cap(ticket)
            except Exception as exc:
                replies, run_started, reason = [], False, str(exc)
            else:
                reason = "no run started"
            if not run_started:
                self.queue.audit("review_continue_blocked", actor="human", ticket_id=tid,
                                 thread_id=review_cap.get("thread_id"), detail={"reason": reason})
                return {"ok": False, "action": "review_continue_blocked", "id": tid,
                        "replies": replies, "error": reason}
            self.queue.done(tid, actor="human", note="review loop continued; count reset")
            return {"ok": True, "action": "continue_review", "id": tid, "replies": replies}
        if ticket["status"] in {"review", "approved"} and agent_task:
            if ticket["status"] == "review":
                ticket = self.queue.approve(tid, actor="human", note="approved agent task")
            try:
                replies, run_started = self.queue.threads.dispatch_approved_agent_task(ticket)
            except Exception as exc:
                replies, run_started = [], False
                reason = str(exc)
            else:
                reason = "no run started"
            if not run_started:
                self.queue.audit(
                    "agent_task_blocked", actor="human", ticket_id=tid,
                    thread_id=agent_task.get("thread_id"), detail={"reason": reason},
                )
                notification = self.queue._notify(
                    f"{tid} agent task blocked; approval remains retryable", source="hitl-reply"
                )
                return {
                    "ok": False, "action": "agent_task_blocked", "id": tid,
                    "replies": replies, "error": reason, "notification": notification,
                }
            self.queue.done(tid, actor="human", note="approved agent task dispatched")
            notification = self.queue._notify(
                f"OK {tid} agent task dispatched", source="hitl-reply"
            )
            return {
                "ok": True, "action": "dispatch_agent_task", "id": tid,
                "replies": replies, "notification": notification,
            }

        if ticket["status"] == "review":
            self.queue.approve(tid, actor="human", note=f"APPROVE → {worker}")
        elif ticket["status"] != "approved":
            notification = self.queue._notify(
                f"{tid} is {ticket['status']}; cannot approve.", source="hitl-reply"
            )
            return {
                "ok": False,
                "error": f"status {ticket['status']}",
                "notification": notification,
            }
        else:
            self.queue.audit(
                "approval", actor="human", ticket_id=tid,
                detail={"decision": "approve", "worker": worker, "already_approved": True},
            )

        from .queue import apply_gate_open

        mode = "apply" if apply_gate_open(self.queue.cfg) else "review_only"
        self.queue.handoff(tid, to=worker, actor="human", mode=mode)
        notification = self.queue._notify(
            f"OK {tid} → {worker} ({mode})", source="hitl-reply"
        )
        return {
            "ok": True,
            "action": "handoff",
            "id": tid,
            "worker": worker,
            "mode": mode,
            "notification": notification,
        }

    def skip(self, ticket_id: str) -> dict:
        tid = ticket_id.upper()
        ticket = self.queue.get(tid)
        review_cap = (ticket.get("payload") or {}).get("review_round_cap")
        self.queue.skip(tid, actor="human", note="SKIP via reply")
        suffix = "; review scope resolved" if review_cap else ""
        notification = self.queue._notify(f"Skipped {tid}{suffix}", source="hitl-reply")
        result = {"ok": True, "action": "skip", "id": tid, "notification": notification}
        if review_cap:
            result["resolved_scope"] = review_cap["scope"]
        return result
