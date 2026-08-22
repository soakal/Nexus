---
name: nexus-brain-batch-debug
description: Read a nightly brain-organizer batch-mode run — batch vs. sync-fallback discrimination from organizer.log and usage.jsonl (success is silent; every "SynthesisBatcher:" log line is a failure path), the lock-mtime heartbeat that separates "normally waiting on Anthropic" from actually wedged, and how to toggle use_batch_api off safely. Use when the nightly run looks slow or stuck, when checking whether batching actually happened, or before killing a run mid-batch.
---

# Reading a brain-organizer batch run

All citations are against `modules/brain-organizer/brain_organizer.py` at `7aa3615` (origin/main,
2026-08-22). Batch mode lives in one class, `SynthesisBatcher` (`:2403`), gated by a single config
key: `use_batch_api`, code default `False` (`:117`), read once at construction (`:2446`). The whole
design self-heals to the synchronous path on every failure — so the first thing to internalize is
that **a healthy batch night leaves almost no trace in the log.**

## Success is silent

Zero `SynthesisBatcher:` lines in `organizer.log` means batch mode worked cleanly. Every one of the
four log lines the class can emit is a failure path:

| Line | Meaning | Severity |
|---|---|---|
| `:2545` | Batch create hard-capped (usage limits) | error, run aborts |
| `:2552` | Batch create transient failure — falling back to sync for the whole flush | warning, self-heals |
| `:2568` | 25h guard exceeded — unreturned items treated as errored | error, self-heals per item |
| `:2603` | Reading batch results failed | warning, self-heals |

If none of these appear for a night, batching happened and nothing needed the fallback path.

## The two positive discriminators

- **The per-route completion line.** The sync/direct branch logs
  `Synthesis complete for route: %s (new=%s)` (`:2780`); the batcher branch logs the same line
  *without* the `(new=...)` suffix (`:2813`). Grep for the suffix to count sync completions:

  ```bash
  ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 '
  grep -c "Synthesis complete for route.*(new=" /opt/nexus/modules/brain-organizer/logs/organizer.log
  grep -c "Synthesis complete for route" /opt/nexus/modules/brain-organizer/logs/organizer.log
  '
  ```

  The difference between the two counts is how many routes went through the batcher.

- **`usage.jsonl`'s `provider` field.** `"anthropic_batch"` for batch results (`:2593-2599`),
  `"anthropic"` for sync fallback (`:1211`), `"openrouter"` for the second-tier fallback, where the
  model keeps its `anthropic/` prefix (`:1293`). Batch waves show up as timestamp-clustered bursts:

  ```bash
  ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 \
    'grep -o "\"provider\": \"[a-z_]*\"" /opt/nexus/modules/brain-organizer/logs/usage.jsonl | sort | uniq -c'
  ```

  **This one is time-sensitive:** NEXUS's `brain_spend_ingest` job claims and deletes `usage.jsonl`
  every 5 minutes (`backend/scheduler.py:820`), so the file may simply not exist by morning. Run
  this check mid-run or within minutes of a write; past that window, use `nexus-brain-spend-verify`'s
  ratio check instead — the same `provider` distinction survives ingestion into spend data, just not
  as a literal column.

## Normal slow vs. actually stuck: the lock mtime is the heartbeat

During a batch wait, the poll loop touches `.organizer.lock` (path set at `:3386`) on every
iteration (`:2561-2565`) at `_POLL_SECONDS = 30.0` (`:2428`). So:

- **PID alive + lock mtime under a minute old = healthy** — waiting on Anthropic. Typical turnaround
  is under an hour; the server guarantees ≤24h. `_MAX_BATCH_WAIT_SECONDS = 25 * 3600` (`:2429`) is
  explicitly a belt-only backstop past that guarantee, not the expected wait.
- **Lock mtime stale while the PID lives = actually wedged** — sync-fallback retry backoff, or stuck
  somewhere outside the batcher entirely.

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 \
  'stat -c "lock mtime: %y" /opt/nexus/modules/brain-organizer/.organizer.lock; date'
```

## Why multiple waves per night is normal

Requests accumulate per group-thread; a flush fires when every still-active group-thread is blocked
waiting or a 30s debounce since the first pending request expires, with the pending list claimed
atomically so a thundering herd at the same timeout submits exactly one batch, not several. A
finished group's thread calls `on_worker_exit` to wake anyone still converging. Files routed to the
same wiki page are serialized within one group, so a second file that shares a page rides a
**second batch wave**, reading what the first file just wrote. Multiple sequential waves per night
is the design working, not a bug — see the class docstring at `:2403-2430` for the full mechanism.

## Toggling batch off safely

`use_batch_api` lives only in the live, untracked `/opt/nexus/modules/brain-organizer/config.json`
on nexus-lxc (code default `False` at `:117`; not in git — see `nexus-remote-python` for why the
cwd matters when reading it). It's read once at run start (`:2446`, gate also at `:3229`), so
flipping it needs no restart — the next run just takes it, restoring the byte-identical
pre-batcher path. Data is never at risk either way: raw files are deleted only after synthesis and
write both succeed, and every individual batch failure already self-heals to the synchronous
`_call_api` chain.

**The trap:** there is no `batches.cancel` call anywhere in this codebase. Killing a stuck run does
**not** stop server-side billing for the in-flight batch — it keeps processing and you pay for it —
and the next run resubmits those same items too, paying twice. Prefer waiting out the ≤24h server
guarantee over killing a run you merely suspect is slow.

## Fast triage

- No `SynthesisBatcher:` lines, `anthropic_batch` present in `usage.jsonl` → worked, done.
- `SynthesisBatcher:` fallback warnings, `anthropic` providers where you expected batch → batch
  failed, work still completed at full price — see `nexus-brain-spend-verify` for the cost impact.
- Lock mtime fresh, hours elapsed → waiting on Anthropic, leave it.
- Lock mtime stale → wedged — investigate the PID before killing, and re-read the billing trap above
  first.

Cross-reference: `nexus-remote-python` for the cwd trap on any DB/log read, and
`modules/brain-organizer/tests/test_synthesis_batcher.py` as the executable reference for the
thundering-herd, lock-touch, per-item fallback, and 25h-guard behavior.
