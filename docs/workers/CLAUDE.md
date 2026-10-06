# Claude worker (apply / ship)

You act only when a ticket is in `handoff` and `handoff.worker` is `claude`
(or the human reply named CLAUDE).

## Each wakeup

1. List / get the handoff ticket. Confirm the packet path.
2. Do the work the packet describes (demo: review_only or synthetic finish).
3. Mark done:

```bash
python3 -m hitl_queue --state-dir state done TKT-ID \
  --actor claude \
  --note "what finished; evidence"
```

## Rules

- No handoff ticket naming you → stop.
- Do not race Codex on the same id.
- If blocked (missing tool, captcha, policy), leave the ticket blocked with a note; do not invent a success.
