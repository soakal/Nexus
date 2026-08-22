---
name: nexus-notify-delivery-trace
description: Answer "why didn't my phone get that alert" — the four-gate order in events.notify_phone (every gate returns False identically, no log line says which fired), the trap that a Telegram 400 from one unescaped character is terminal and never retried while 429/5xx queue and retry, and the ExpectedDelivery heartbeat layer with its auto-register-forever behavior. Use when an expected page never arrived, a recovery notice is missing, or a "delivery overdue" page needs interpreting.
---

# Tracing a phone notification

The send path is `backend/events.py::notify_phone` (`:46`) → `backend/integrations/telegram.py`'s
`notify`. (`events.py` sits directly under `backend/`, not `backend/agents/` — easy to grep the
wrong directory.) Citations at `7aa3615`, 2026-08-22. The core problem this skill exists for:
**every gate returns False identically from the caller's perspective — no single log line tells
you which one ate the message.**

## The gate order

1. `phone_notifications_enabled` (`:62`) — validated against the Telegram secrets at config load.
2. Static `phone_suppressed_kinds` from `.env` (`:64-65`).
3. The runtime `/mute` set (`:67-79`) — read in its own try/except that deliberately fails **open**:
   a DB hiccup degrades to "not muted," never to "alert dropped."
4. `telegram.notify` itself.

Upstream of all four: `should_page`/calibration suppression can stop the flag from paging at all
(see `nexus-flag-calibration-lifecycle`), and `homelab_watch`'s in-memory `_paged_alerts` latch
(`homelab_watch.py:46`) means a recovery notice only fires for an alert that actually paged — and
the whole latch resets on process restart, so a restart mid-incident silently cancels the pending
recovery notice.

## The headline trap: a 400 is terminal, retries are for 429/5xx only

`_TERMINAL_STATUS_CODES = {400, 401, 403, 404}` (`telegram.py:25`) — logged at ERROR and dropped,
never queued.

`notify_phone` switches the whole message to `parse_mode="HTML"` whenever `app_base_url` is set, to
append a clickable "Open Safety" deep link (`events.py:82-94`). So one unescaped `&` or `<` in
interpolated free text makes the **entire message** a 400: gone, indistinguishable from being
muted.

Known escaping state: `homelab_digest.py` and `homelab_watch.py` `html.escape` their interpolations
throughout. The daily digest's goal-title lines (`digest.py:205`, `:214`, `:223`) are **not**
escaped — a known, in-code-acknowledged gap (see the comment at `digest.py:280-285`) — while the
same function's failure-reason and proposer-filter lines (`:238`, `:303-306`) are. A goal titled
with a stray `<` or `&` will eat that day's digest.

Retryable failures (429/5xx/network) queue into `PendingDelivery` (`:341-343`), retried on a 60s
cadence with exponential backoff — base 60s, cap 3600s (`:30-31`) — and dead-lettered at 8 attempts
(`:32`). Dead letters page once via a DB-backed cooldown that survives restarts
(`watchdog.py:16-17`, `_should_alert_dead_letters_db` `:50`, `check_dead_letters` `:163-180`).

`telegram.delivery_queue_health()` (`:419`) is the one-call triage read:

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
import asyncio
from backend.integrations.telegram import delivery_queue_health
print(asyncio.run(delivery_queue_health()))
PY
```

(cwd matters — see `nexus-remote-python`.) Returns `pending_count`, `oldest_age_seconds`,
`dead_lettered_count`, `secret_present`.

## The ExpectedDelivery heartbeat layer

`backend/agents/deliveries.py`. Any authed `POST /api/deliveries/{name}/heartbeat` auto-registers a
**permanent** row on first contact (`record_heartbeat`, `:97-98`; `_db_heartbeat` `:58-77`), with
daily+2h defaults (`DEFAULT_INTERVAL_MINUTES = 1440`, `DEFAULT_GRACE_MINUTES = 120`, `:44-45`) that
pages forever once overdue (`is_overdue`, `:155-165`). A typo'd name therefore creates an eternal
nag; `delete_delivery` (`:143-146`) is the escape hatch, and `update_delivery` (`:149-151`) is the
only way to tune the interval/grace.

Currently wired: `brain_organizer` posts its heartbeat at run end (`brain_organizer.py:3001`,
called at `:3064`). **Not** yet wired: the devbox digest-relay cron —
`tools/relay_claude_digest.py` contains no heartbeat call at all — so a "delivery overdue" page
about that pipeline specifically cannot exist yet, and the absence of one there currently means
nothing.

## Fast triage

- Alert missing, other alerts of the same kind arrive fine → suspect a 400. Check the app log for
  the Telegram ERROR line and eyeball the message text for a stray `&` or `<`.
- All alerts missing → gate 1, or check `secret_present` in `delivery_queue_health()`.
- One kind missing → gates 2/3 (`phone_suppressed_kinds`, then the `/mute` list).
- Alerts delayed in bursts → the `PendingDelivery` queue draining after 429/5xx — check
  `oldest_age_seconds`.
- Missing recovery notice right after a restart → the `_paged_alerts` latch reset, expected.
- A "delivery overdue" page → a real producer miss, or a typo'd auto-registered name — list
  deliveries and `delete_delivery` the typo.
- A flag-based page missing entirely → this is upstream of everything above, go to
  `nexus-flag-calibration-lifecycle`.
