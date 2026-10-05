# Example: day-job packet lane (synthetic)

This is one shape the queue supports. The public demo uses fake employers only.

```text
mail / hunt scores Apply
  → enqueue job_draft
  → phone: New TKT-0001 …
  → human: APPROVE TKT-0001 CLAUDE   # worker pick early
  → draft agent claims, writes resume/letter, submit-draft
  → auto handoff to Claude (review_only or apply by date gate)
  → Claude finishes → done
  → progress texts on each stage; stale nudge if a stage sits idle
```

Do not publish private mail, real applicant data, or employer secrets into this
repo. Keep operator state local.
