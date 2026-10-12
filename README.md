# hitl-agent-queue

**Coordinate several coding agents with one human gate.**

Cursor drafts. Claude or Codex applies / ships. You approve from your phone.
Durable tickets remain the work bus; bounded, audited threads are available
for explicit agent turns.

This is a **public, synthetic** reference for the same playbook used in a
private operator setup. It ships:

- a status machine (`new → drafting → review → approved → handoff → done`)
- role claims (`cursor-draft`, `claude`, `codex`, …)
- a pluggable notifier (stdout by default; Slack / Teams / Discord / webhook / Cortland optional)
- reply grammar: `APPROVE TICKET-ID CODEX|CLAUDE` / `SKIP TICKET-ID`
- optional reply server for Slack slash commands and Teams/Zapier ingress
- a synthetic end-to-end demo you can run locally
- durable user/agent threads with run limits, pause/resume/kill, and audit logs

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

## Agent threads

Threads use the same `queue.sqlite3` database. Configure adapters from the
existing TOML file, then post explicit mentions:

```python
queue.threads.configure_from("agents.toml")
thread = queue.threads.create("Release notes", max_agent_runs=6)
queue.threads.post(thread["id"], "user", "@codex task: summarize the changes")
queue.threads.post(thread["id"], "user", "@codex fyi: no action needed")
```

`task:` wakes an active adapter; `fyi:` never does. `pause`, `resume`, and
`kill` accept an agent name or no name for all agents. Each adapter keeps its
configured timeout, and the thread run budget bounds agent-to-agent turns.

Every agent entry must set an explicit `work_dir` inside one of its
`allowed_folders`. The queue state directory must be outside—and must not
overlap—any agent work folder. This includes symlinks in `work_dir` that point
at the state directory or one of its ancestors. `allowed_folders` validates
only the starting directory; the approval gate depends on the invoked agent CLI's own sandbox
and permission settings to prevent filesystem or network access.

Agent-authored `@name task:` mentions require human approval by default and
self-tasking is always rejected. Set `allow_agent_to_agent = true` on the
target agent, or on a thread at creation time, only when that relay is wanted.

Agents cannot directly request irreversible or outbound work. They emit one
JSON line, which creates a normal ticket in `review` for the existing
approve/skip flow:

```text
ACTION_REQUEST: {"action":"push","summary":"publish the reviewed branch","target":"origin"}
```

Allowed action names are `push`, `merge`, `send`, and `network`. Messages,
run start/end results and durations, and approval decisions are copied into
an append-only `audit_log` table.

### Usage plans and routing

Each completed thread run is also recorded in local SQLite with its start,
duration, outcome, and adapter-reported token usage. Unknown token counts stay
unknown. Add an optional plan to an agent in `agents.toml`:

```toml
[agents.codex.plan]
window = "5h" # rolling; or "daily" / "weekly" (UTC)
runs = 40
minutes = 240
tokens = 1000000
warn_threshold = 0.8
blocked_threshold = 1.0
```

Caps may be omitted independently. Warnings go through the normal notifier
(and respect mute), are audited once per threshold/window, and runs stop at
the blocked threshold. CLI adapters may set `usage_regex` (named token groups
are supported) or `usage_json_field`; OpenAI-compatible adapters read common
response usage fields automatically.

```python
queue.usage.summary()  # per-agent app/read API data
queue.threads.route("summarize changes", ["codex", "claude"])
```

The router skips paused, killed, and quota-blocked agents, then chooses the
greatest remaining quota headroom with agent-name tie breaking.

## Desktop board (demo data)

```bash
pip install -e ".[app]"          # optional: pywebview window
python3 -m hitl_queue app        # falls back to your browser without pywebview
python3 -m hitl_queue app --browser --apply-opens 2026-01-01
```

The app seeds synthetic tickets into a throwaway state dir (it ignores
`--state-dir`). It shows one column per status, a collapsed
blocked/skipped/cancelled lane, ticket files and status history, and the
`apply_opens` gate. Approve and Skip buttons call `hitl_queue.actions`.
Notifications go to a local outbox panel with a mute toggle and are never
sent.

## Desktop app (macOS)

Build the standalone desktop app with the project-local virtualenv:

```bash
make app
open "dist/HITL Queue.app"
```

The build is unsigned. The first time you launch it, right-click
`HITL Queue.app` and choose **Open** to approve it in macOS.

## Tray app

The optional tray/menu-bar app shows review tickets awaiting approval, running
agents, and quota bars. Approve and Skip operate on the configured public queue;
**Open app** starts the existing synthetic desktop board in a separate process.

```bash
pip install -e ".[tray]"
python3 -m hitl_queue tray                 # uses --state-dir (default: state)
python3 -m hitl_queue tray --demo          # neutral throwaway demo data only
```

- **macOS:** `make tray-app`, then `open "dist/HITL Tray.app"`. The unsigned
  app may require right-clicking it and choosing **Open** once. `LSUIElement` is
  enabled, so the bundle appears only in the menu bar.
- **Windows:** install the tray extra and run the command above, or build with
  `pyinstaller --noconfirm --clean packaging/hitl_tray.spec`; launch
  `dist/HITL Tray.exe`.
- **Linux:** install a system tray implementation and GI bindings first. On
  Debian/Ubuntu: `sudo apt install libappindicator3-dev
  gir1.2-appindicator3-0.1 python3-gi`. Some desktop environments also require
  an AppIndicator extension. Build with the same PyInstaller command and launch
  `dist/HITL Tray`.

The workflow in `.github/workflows/tray-build.yml` builds all three platforms
and uploads their unsigned artifacts; it can be started manually or by a `v*`
tag.

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

- Not an unbounded or unaudited agent chat relay
- Not an ATS scraper or auto-apply bot for the public demo
- Not your private second brain, mail, or credentials
- Not a substitute for reading the ticket before you approve

## Portfolio framing

Built as a public reference for AI workflow / internal automation work:
durable state, human approval, role handoffs, and auditable progress — the
same shape as production agent loops with a stop the operator can trust.

## License

MIT
