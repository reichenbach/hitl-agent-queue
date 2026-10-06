# hitl-agent-queue

**Coordinate several coding agents with one human gate.**

Cursor drafts. Claude or Codex applies / ships. You approve from your phone.
No agent-to-agent chat relay. No shared brain crawl. A durable ticket board
is the bus.

This is a **public, synthetic** reference for the same playbook used in a
private operator setup. It ships:

- a status machine (`new → drafting → review → approved → handoff → done`)
- role claims (`cursor-draft`, `claude`, `codex`, …)
- a pluggable notifier (stdout by default; Slack / Teams / Discord / webhook / Cortland optional)
- reply grammar: `APPROVE TICKET-ID CODEX|CLAUDE` / `SKIP TICKET-ID`
- optional reply server for Slack slash commands and Teams/Zapier ingress
- a synthetic end-to-end demo you can run locally

Stdlib Python 3. No cloud required for the queue itself.

## Agent roles

Cursor / Claude / Codex / Grokbot use the same board with different claims.
Start at [AGENTS.md](AGENTS.md). Per-role sheets live under
[docs/workers/](docs/workers/).

## Why this exists

Multi-agent work fails in the same boring ways: duplicate effort, silent
stalls, and a human copy-pasting context between tools. The fix is not a
bigger chat. It is:

1. **Compile once** into a ticket
2. **Claim** by role
3. **Human gate** on irreversible steps
4. **Handoff** with a frozen packet
5. **Progress pings** so silence means stuck, not success

That is ordinary workflow engineering. The agents are just the workers.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python3 -m hitl_queue demo
python3 -m hitl_queue list
python3 -m unittest discover -s tests -v
```

## Status machine

```text
new → drafting → review → approved → handoff → done
              ↘ skipped / cancelled / blocked
```

| Status | Who | Meaning |
|---|---|---|
| `new` | dispatcher | Waiting for a draft worker |
| `drafting` | draft agent | Materials in progress |
| `review` | human | Ready for approve / skip |
| `approved` | human | OK to hand off |
| `handoff` | apply / ship worker | Frozen packet owned by one role |
| `done` | worker | Finished with evidence |

## Reply grammar (human gate)

When a ticket hits `review`, the notifier sends a short summary. Reply:

```text
APPROVE TKT-0001 CODEX
APPROVE TKT-0001 CLAUDE
SKIP TKT-0001
```

Approve while still `new` / `drafting` stores a **preferred worker** and acks.
When the draft lands in `review`, handoff can use that pick automatically.

## Optional chat channels

iMessage/Cortland is one option. Slack, Teams, Discord, or any webhook work
without Apple gear. Chat apps are transport only — the ticket board stays
the source of truth.

```bash
export HITL_NOTIFY=slack          # or teams | discord | webhook | cortland | multi
export HITL_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
# Approvals: CLI always works; or run a local reply ingress:
export HITL_REPLY_TOKEN='long-random-string'
python3 -m hitl_queue reply-server --port 8787
# /hitl APPROVE TKT-0001 CLAUDE   (Slack slash → reply-server)
```

Full setup: [docs/CHANNELS.md](docs/CHANNELS.md).

## What this is not

- Not a framework that makes agents talk to each other
- Not an ATS scraper or auto-apply bot for the public demo
- Not your private second brain, mail, or credentials
- Not a substitute for reading the ticket before you approve

## Portfolio framing

Built as a public reference for AI workflow / internal automation work:
durable state, human approval, role handoffs, and auditable progress — the
same shape as production agent loops with a stop the operator can trust.

## License

MIT
