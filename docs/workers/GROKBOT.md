# Grokbot worker (non-apply)

You take `site` / `outreach` style tickets only. Never job applications in
this public demo. Never claim apply/ship handoffs meant for Claude or Codex.

## Each wakeup

```bash
python3 -m hitl_queue --state-dir state claim --worker grokbot
# ... do the site/outreach work ...
python3 -m hitl_queue --state-dir state done TKT-ID --actor grokbot --note "what shipped"
```

## Rules

- Stay out of the draft → human approve → apply lane unless the ticket kind
  is explicitly non-apply.
- Public surfaces stay synthetic and credential-free.
