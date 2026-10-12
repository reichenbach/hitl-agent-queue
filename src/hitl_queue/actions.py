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
            self.queue._notify(
                f"Got it. {tid} → {worker} after draft. Reply SKIP {tid} to cancel.",
                source="hitl-reply",
            )
            return {"ok": True, "action": "prefer_worker", "id": tid, "worker": worker}

        if ticket["status"] == "review":
            self.queue.approve(tid, actor="human", note=f"APPROVE → {worker}")
        elif ticket["status"] != "approved":
            self.queue._notify(
                f"{tid} is {ticket['status']}; cannot approve.", source="hitl-reply"
            )
            return {"ok": False, "error": f"status {ticket['status']}"}

        from .queue import apply_gate_open

        mode = "apply" if apply_gate_open(self.queue.cfg) else "review_only"
        self.queue.handoff(tid, to=worker, actor="human", mode=mode)
        self.queue._notify(f"OK {tid} → {worker} ({mode})", source="hitl-reply")
        return {
            "ok": True,
            "action": "handoff",
            "id": tid,
            "worker": worker,
            "mode": mode,
        }

    def skip(self, ticket_id: str) -> dict:
        tid = ticket_id.upper()
        self.queue.skip(tid, actor="human", note="SKIP via reply")
        self.queue._notify(f"Skipped {tid}", source="hitl-reply")
        return {"ok": True, "action": "skip", "id": tid}
