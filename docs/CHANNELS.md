# Chat channels (optional)

Outbound progress and review asks go through a **notifier**. Inbound approvals
use the same grammar everywhere:

```text
APPROVE TKT-0001 CODEX
APPROVE TKT-0001 CLAUDE
SKIP TKT-0001
```

## Choose a notifier

| `HITL_NOTIFY` | Needs | Notes |
|---|---|---|
| `stdout` (default) | nothing | CI-safe |
| `cortland` | `CORTLAND_IMESSAGE_CLI` | Apple Messages via [Cortland](https://github.com/cortland-suite/cortland) |
| `slack` | `HITL_SLACK_WEBHOOK_URL` | Incoming Webhook |
| `teams` | `HITL_TEAMS_WEBHOOK_URL` | Incoming Webhook / Power Automate |
| `discord` | `HITL_DISCORD_WEBHOOK_URL` | Channel webhook |
| `webhook` | `HITL_WEBHOOK_URL` | POST `{"text","source"}` to n8n/Zapier/custom |
| `multi` | `HITL_NOTIFY_CHANNELS=stdout,slack` | Fan-out |

```bash
export HITL_NOTIFY=slack
export HITL_SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
python3 -m hitl_queue enqueue --title "Example" --company "Acme"
```

```bash
export HITL_NOTIFY=multi
export HITL_NOTIFY_CHANNELS=stdout,teams
export HITL_TEAMS_WEBHOOK_URL='https://...'
```

## Approvals without iMessage

**Option A — CLI (always works)**

```bash
python3 -m hitl_queue reply "APPROVE TKT-0001 CLAUDE"
```

**Option B — reply server** (Slack slash command, Zapier, Teams flow)

```bash
export HITL_REPLY_TOKEN='long-random-string'
python3 -m hitl_queue reply-server --host 127.0.0.1 --port 8787
```

POST examples:

```bash
# generic JSON
curl -s -X POST http://127.0.0.1:8787/ \
  -H "Content-Type: application/json" \
  -H "X-Hitl-Token: $HITL_REPLY_TOKEN" \
  -d '{"text":"APPROVE TKT-0001 CLAUDE"}'

# Slack slash command style (form)
curl -s -X POST http://127.0.0.1:8787/ \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "token=$HITL_REPLY_TOKEN&text=SKIP+TKT-0001"
```

Put the reply server behind your VPN / Tailscale / Cloudflare Tunnel. Do not
expose it to the public internet without auth.

## Slack slash command sketch

1. Create a Slack Incoming Webhook for outbound (`HITL_NOTIFY=slack`).
2. Create a Slash Command e.g. `/hitl` that POSTs to your reply server URL.
3. Usage in Slack: `/hitl APPROVE TKT-0001 CLAUDE`

## Teams sketch

1. Incoming webhook for outbound cards (`HITL_NOTIFY=teams`).
2. Power Automate flow: when you reply in a channel with the grammar, HTTP POST
   to the reply server. Or approve from the CLI while traveling.

## Design rule

Chat apps are **transport**. The ticket board is the source of truth. Do not
put business logic in Slack workflows beyond forwarding the reply line.
