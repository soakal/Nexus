---
name: nexus-collector-trace
description: Trace a "N cached state item(s) stale or unavailable" dashboard banner to the actual failing collector — the banner counts but never names, the `dashboard.<short>` collector keys in state_workers.py don't visibly match the `source`/integration names used elsewhere (e.g. `dashboard.channels` -> response key `channels` -> integration `channels_dvr`), the only real error line is a repeating WARNING in `journalctl -u nexus-backend`, and an integration's config default in backend/config.py can be silently overridden by the `.env` in the systemd unit's WorkingDirectory (`/var/lib/nexus/.env`), not the repo checkout's. Use when the dashboard shows stale/unavailable cached state, a collector warning loops in the journal, or an integration's upstream needs to be curled directly to split network vs config vs the upstream service itself.
---

# nexus-collector-trace

The dashboard's "N cached state item(s) stale or unavailable" banner (frontend/src/pages/Dashboard.jsx)
is a bare count — `staleCount`, computed over `Object.values(sources)` freshness plus
`Object.values(stateFreshness)` — with no indication of which item. Finding the actual culprit means
walking three separate naming layers that don't visibly map to each other, then reading the one place
the real error is ever logged.

## 1. Map the banner to a collector

Every background poller is registered in `backend/state_workers.py` as either:

- `_source(key, seconds)` -> `Collector("source.<key>", ...)` — feeds the `sources` dict (the "N/N
  online" pill), via `_source_health`.
- `Collector("dashboard.<short>", seconds, lambda: _fetch("<key>"))` — feeds the actual data card
  shown in the UI, via `_fetch`.

The *same* integration is usually behind both, under two different keys. `backend/api/dashboard_state.py`
then re-keys the `dashboard.<short>` collectors again into the JSON response (`item("<response_key>")`),
and the frontend re-keys that a third time into `stateFreshness`. As of 2026-09-05 the full chain:

| `source.<key>` (health pill) | `dashboard.<short>` (data card) | API response / `stateFreshness` key | integration module |
|---|---|---|---|
| homeassistant | — | — | `integrations/homeassistant.py` |
| unifi | — | — | `integrations/unifi.py` |
| unraid | dashboard.unraid | unraid | `integrations/unraid.py` |
| obsidian | — | — | `integrations/obsidian.py` |
| github | — | — | `integrations/github.py` |
| openrouter | dashboard.openrouter | openrouter | `integrations/openrouter.py` |
| weather | dashboard.weather | weather | `integrations/weather.py` |
| channels_dvr | dashboard.channels | channels | `integrations/channels_dvr.py` |
| adguard | dashboard.adguard | adguard | `integrations/adguard.py` |
| proxmox | dashboard.proxmox, dashboard.proxmox_maintenance | proxmox, proxmox_maintenance | `integrations/proxmox.py` |
| protonmail | dashboard.mail | mail | `integrations/protonmail.py` |
| calendar | dashboard.today | today | `integrations/calendar.py` |
| — | dashboard.brain | brain | `agents/brain_organizer` status |
| — | dashboard.briefing | briefing | latest-briefing lookup |
| — | dashboard.claude_usage | claude_usage | Claude usage capture (statusline; staleness here is *normal*, see the note in Dashboard.jsx) |

If this table has drifted (a 13th integration added since), regenerate it from
`grep -n 'Collector(\|_source(' backend/state_workers.py`, `REGISTRY_NAMES` in `backend/api/sources.py`,
and the `item(...)` calls in `backend/api/dashboard_state.py` — `backend/safety/contracts.py` has its own
drift assertion (`set(sources) == set(REGISTRY_NAMES)`) that fails loudly if `sources.py` itself falls
out of sync, but nothing enforces the state_workers.py <-> dashboard_state.py <-> Dashboard.jsx naming
staying aligned.

## 2. Find the real error

Nothing on the dashboard or in the API response says why a collector is failing — only
`journalctl -u nexus-backend` does, as a WARNING that repeats every collector interval forever (an old
timestamp does not mean it stopped, and a *recent* one does not mean it just started):

```
journalctl -u nexus-backend --since '30 min ago' --no-pager | grep 'State collector'
```

Look for `State collector dashboard.<short> failed: <exception message>` — `<short>` is the collector
key from step 1, and `<exception message>` is usually the actual root cause (an HTTP status, a
connection error, a JSON shape mismatch) without needing to read the integration source at all.

## 3. Resolve the integration's real config

The integration module (`backend/integrations/<name>.py`) reads its config through
`backend/config.py`'s `get_settings()`, which has a hardcoded default (e.g.
`channels_host: str = "http://localhost:8089"`). That default is commonly overridden — silently, with
no log line saying so — by a `.env` file. Which `.env` is real depends on the **systemd unit's**
`WorkingDirectory`, not the repo checkout:

```
grep WorkingDirectory /etc/systemd/system/nexus-backend.service   # -> /var/lib/nexus
grep -i <integration_name> /var/lib/nexus/.env                    # the ACTUAL value in effect
```

`/opt/nexus/.env` (the repo checkout) is a red herring — it's not what the running service reads.

**If you change `/var/lib/nexus/.env`, it does nothing until you restart the service.**
`get_settings()` (`backend/config.py:741-747`) is a module-level singleton — `Settings()` reads `.env`
once, on first call, and every later call returns that same cached instance for the life of the
process. A `.env` edit with no restart is invisible: `systemctl restart nexus-backend` first, then move
on to verifying.

## 4. Curl the upstream directly

Once the real host/port is known, curl it directly with a timeout — a genuinely dead host **hangs**
rather than refusing, so an untimed curl can look identical to "still loading":

```
curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://<real-host>:<port>/<endpoint>
```

Three distinct outcomes, and they point at three different fixes:

- **Connection refused / timeout** — network problem (host down, firewall, wrong port).
- **Non-2xx from the right host with a body worth reading** — curl the endpoint again without
  `-o /dev/null` and read the response body; the upstream service is often telling you exactly what's
  wrong (e.g. Channels DVR returning `{"error":"DVR not enabled"}` on `/api/v1/jobs` — a feature
  disabled on *its* end, not a NEXUS bug).
- **200 from curl but the collector still fails** — the integration's parsing/shape assumptions are
  wrong for the current upstream response; read `backend/integrations/<name>.py` directly.

## 5. Verify the fix

If the fix was purely upstream (step 4's first two outcomes with no config change), the warning stops
appearing on the *next* collector interval with no restart needed — the collector keeps polling on its
own schedule. If the fix touched `.env` (step 3), restart first (`systemctl restart nexus-backend`) —
otherwise you're watching the journal for a config that was never actually reloaded, and a repeating
warning after your "fix" means the restart is still owed, not that the fix failed. Either way, tail
`journalctl -u nexus-backend -f` and confirm one full collector interval passes with no new
`State collector <short> failed` line before calling it fixed.
