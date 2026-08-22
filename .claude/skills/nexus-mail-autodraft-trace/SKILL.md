---
name: nexus-mail-autodraft-trace
description: Answer "why didn't NEXUS draft a reply to X", "why did it trash Y", or "why does it keep ignoring Z" — the ProcessedMailId claim is a permanent verdict (a KEEP, a failed dispatch, or autotrash-off at classification time all skip that email forever), while per-tick caps deliberately leave candidates unclaimed for next tick. Use when an email never got a draft, a trash move did or didn't happen, or old mail isn't being revisited after a settings change.
---

# Tracing mail autodraft decisions

One entry point: `backend/agents/mail_drafts.py::autodraft_tick`. Citations at `7aa3615`,
2026-08-22. The single most important object is the `ProcessedMailId` row — nearly every
"why is this email ignored" question is answered by its existence, not by the classifier.

## The headline trap: claimed = skipped forever, unclaimed = reconsidered

The `ProcessedMailId` claim is a **permanent verdict**. Three paths claim an email with zero future
LLM cost:

- A KEEP verdict, or a failed trash dispatch — "claimed, permanent skip either way" (`:478-479`).
- `mail_autotrash_enabled=False` at classification time — every automated-sender email seen while
  the feature is off is claimed unseen (`:443-446`).
- A missing junk profile — the None-profile branch also claims each automated email it passes over
  (`:452-456`), not just skips trashing it that tick.

Consequences worth stating plainly: **enabling autotrash later never revisits old mail**, and an
email classified SKIP once is never reconsidered.

By contrast, hitting a per-tick cap — `MAX_CLASSIFIED_PER_TICK = 10` (`:18`) or
`MAX_TRASHED_PER_TICK = 5` (`:20`) — deliberately leaves candidates **unclaimed**, reconsidered next
tick (`:458-460`, `:481-484`).

**Diagnostic: check the email's `ProcessedMailId` row (and its `drafted`/`trashed` flags) before
reading a single line of classifier logic.**

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from sqlmodel import Session, select
from backend.database import ProcessedMailId, engine
with Session(engine) as s:
    row = s.exec(select(ProcessedMailId).where(ProcessedMailId.email_id == "YOUR_EMAIL_ID")).first()
    print(row)
PY
```

(cwd matters — see `nexus-remote-python`. No row = never claimed, will be reconsidered next tick.)

## Trashing genuine mail is structurally impossible

`_is_automated_sender` (`:33`) splits every email into two disjoint paths at classification
(`:442`): automated senders can only ever become trash candidates, human-looking senders can only
become draft candidates. There is no code path from a human sender to Trash.

Trash still routes through the broker as `protonmail_delete`, `actor="autonomous"`, and only an
`EXECUTED` decision marks the row trashed. So a trash that *didn't* happen may be an action-gating
question, not a mail question — cross-reference `nexus-action-trace`.

## The two profile fallbacks are asymmetric

- **Voice profile** falls back to the fabricated `DEFAULT_VOICE = "Concise, friendly, plain-text
  replies."` (`:17`, returned at `:141`) — drafts still happen, just generically voiced.
- **Junk profile** falls back to `None` (`get_junk_profile`, `:243-251`) — no trashing at all that
  tick, because a guessed junk profile would cause real Trash moves (in-code rationale at
  `:244-247`). Remember from the section above: this branch still permanently claims the emails it
  skips.

Both are Sonnet-distilled singleton rows, refreshed on staleness: `VOICE_REFRESH_DAYS = 14`
(`:16`), `JUNK_REFRESH_DAYS = 30` (`:19`). A stale-rebuild failure silently reuses the old summary
(`:139`).

## Three off-switches that look identical

"No drafts appeared" has three distinct causes that present identically from the outside:

1. The tick honors the kill switch directly — `autonomy_enabled` false skips the whole tick
   (`:414-415`, rationale in the docstring at `:396-398`).
2. The scheduler pause stops the tick from firing at all.
3. The broker's own kill-switch check blocks the trash dispatch.

Tell them apart by evidence: no tick log line at all → scheduler; "skipped: autonomy disabled" log
line → the kill switch at the tick itself; `ProcessedMailId` claimed but `ActionLog` shows
`forbidden` → the broker.

## Fast triage

- No draft for email X → does a `ProcessedMailId` row exist? Claimed-not-drafted means the
  classifier said no-reply-warranted, or dispatch failed — permanently. No row means it hasn't been
  seen yet (caps, or the tick hasn't run) and will be retried.
- Autotrash newly enabled but old junk mail untouched → expected. Only new mail is affected; old
  claims are permanent.
- Something got trashed that shouldn't have → it was an automated-sender match plus a junk-profile
  hit — check `ActionLog` for the `protonmail_delete` row (`nexus-action-trace`).
- Nothing trashed for a whole tick → junk profile is `None`, or `MAX_TRASHED_PER_TICK` was hit — the
  log line distinguishes them.
