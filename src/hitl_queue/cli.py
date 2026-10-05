"""CLI for hitl-agent-queue."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .queue import Queue


def _q(args) -> Queue:
    return Queue(Path(args.state_dir))


def cmd_demo(_: argparse.Namespace) -> int:
    """Synthetic walkthrough printed to stdout."""
    with tempfile.TemporaryDirectory() as tmp:
        q = Queue(Path(tmp), config={"apply_opens": "2099-01-01", "notify_progress": True})
        t = q.enqueue(
            title="AI Workflow Engineer",
            company="Example Health",
            url="https://example.com/jobs/1",
            kind="job_draft",
            reason="Synthetic lane match",
        )
        print("--- prefer worker early ---")
        print(q.handle_reply(f"APPROVE {t['id']} CLAUDE"))
        print("--- claim + draft ---")
        q.claim(t["id"], "cursor-draft")
        resume = Path(tmp) / "resume.txt"
        resume.write_text("Synthetic resume\n", encoding="utf-8")
        out = q.submit_draft(
            t["id"],
            actor="cursor-draft",
            resume=str(resume),
            research_summary="Fits remote workflow lane.",
        )
        print("status after draft:", out["status"])
        print("handoff:", (out.get("payload") or {}).get("handoff"))
        print("--- apply worker finishes ---")
        print(q.done(t["id"], actor="claude", note="review_only complete").get("status"))
        q.close()
    return 0


def cmd_reply_server(args: argparse.Namespace) -> int:
    from .reply_server import serve

    serve(state_dir=args.state_dir, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hitl-queue", description=__doc__)
    p.add_argument("--state-dir", default="state")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="Run a synthetic end-to-end demo")
    sub.add_parser("list")
    s = sub.add_parser("enqueue")
    s.add_argument("--title", required=True)
    s.add_argument("--company", default="")
    s.add_argument("--url", default="")
    s.add_argument("--reason", default="")
    s = sub.add_parser("claim")
    s.add_argument("--worker", required=True)
    s.add_argument("id", nargs="?")
    s = sub.add_parser("submit-draft")
    s.add_argument("id")
    s.add_argument("--resume", default="")
    s.add_argument("--cover-letter", default="")
    s.add_argument("--research", default="")
    s = sub.add_parser("reply")
    s.add_argument("text")
    s = sub.add_parser(
        "reply-server",
        help="HTTP ingress for Slack slash / Teams / webhook approvals",
    )
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s = sub.add_parser("done")
    s.add_argument("id")
    s.add_argument("--actor", default="worker")
    s.add_argument("--note", default="")
    s = sub.add_parser("get")
    s.add_argument("id")

    args = p.parse_args(argv)
    if args.cmd == "demo":
        return cmd_demo(args)
    if args.cmd == "reply-server":
        return cmd_reply_server(args)

    q = _q(args)
    try:
        if args.cmd == "list":
            out = q.list()
        elif args.cmd == "enqueue":
            out = q.enqueue(
                title=args.title,
                company=args.company,
                url=args.url,
                reason=args.reason,
            )
        elif args.cmd == "claim":
            out = q.claim(args.id, args.worker)
        elif args.cmd == "submit-draft":
            out = q.submit_draft(
                args.id,
                actor="cursor-draft",
                resume=args.resume,
                cover_letter=args.cover_letter,
                research_summary=args.research,
            )
        elif args.cmd == "reply":
            out = q.handle_reply(args.text)
        elif args.cmd == "done":
            out = q.done(args.id, actor=args.actor, note=args.note)
        elif args.cmd == "get":
            out = q.get(args.id)
        else:
            out = {"error": "unknown"}
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        q.close()


if __name__ == "__main__":
    raise SystemExit(main())
