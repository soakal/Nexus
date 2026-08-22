---
name: nexus-brain-spend-verify
description: Confirm the Batch API 50% discount (and cache-token pricing) actually landed in NEXUS spend data — the provider marker does not survive ingestion into SpendLog, so batch vs. sync is verified by recomputing the price ratio, not by reading a column; plus the unknown-model trap that silently reports a whole night as $0.00. Use when checking what a brain-organizer night cost, whether batching saved money, or when a night's spend looks suspiciously low or zero.
---

# Verifying brain-organizer spend

Citations against `backend/agents/brain_spend.py` at `7aa3615` (2026-08-22). The pipeline: the
organizer appends usage lines to `usage.jsonl`; `ingest_brain_spend` atomically claims the file as
`usage.jsonl.ingest` (`:28`) and converts each line into a `SpendLog` row with
`label="brain_organizer"`.

## The provider marker does not survive ingestion

`"provider"` is parsed from each usage line (`:117`) but consumed only by `_price_model`
(`:121-126`), which bakes the 0.5x batch discount straight into `cost_usd` (`:67-68`). The
`SpendLog(...)` row built at `:127-137` carries no provider field, and `SpendLog` itself
(`backend/database.py:239-251`) has no such column. **Verify by ratio, not by column.**

The one exception: OpenRouter fallback rows *are* identifiable by column — their `model` keeps the
`anthropic/` prefix.

## The ratio check

```bash
ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /var/lib/nexus && PYTHONPATH=/opt/nexus /opt/nexus/venv/bin/python -' <<'PY'
from datetime import datetime, timedelta
from sqlmodel import Session, select
from backend.database import SpendLog, engine
from backend.agents.router import _PRICE_PER_MTOK

since = datetime.utcnow() - timedelta(hours=18)
with Session(engine) as s:
    rows = s.exec(
        select(SpendLog)
        .where(SpendLog.label == "brain_organizer")
        .where(SpendLog.created_at >= since)
    ).all()

for r in rows:
    model = r.model.split("/", 1)[-1] if "/" in r.model else r.model
    price = _PRICE_PER_MTOK.get(model)
    if price is None:
        print(f"{r.id:5d}  NO PRICE ENTRY for {r.model!r} — {r.input_tokens} in / {r.output_tokens} out")
        continue
    full = (
        r.input_tokens / 1e6 * price["input"]
        + r.output_tokens / 1e6 * price["output"]
        + r.cache_creation_input_tokens / 1e6 * price["input"] * 1.25
        + r.cache_read_input_tokens / 1e6 * price["input"] * 0.10
    )
    ratio = (r.cost_usd / full) if full else 0
    print(f"{r.id:5d}  ratio={ratio:.2f}  cost=${r.cost_usd:.4f}  model={r.model}")
PY
```

(cwd matters — see `nexus-remote-python`.) Read the printed ratios: **~0.5 = batch, ~1.0 = sync
Anthropic fallback.** Group and count to see the night's actual batch/sync mix.

## The $0.00 trap

An unknown model returns `0.0` from `_price_model` — the early return when `price is None`
(`:56-60`) — but the tokens are still recorded on the row. A `sonnet_model` bump in
`config.json` without a matching `_PRICE_PER_MTOK` entry makes a whole night's spend silently
report as free. **A suspiciously-zero night means check the price table first, not the
organizer.** The script above already flags this explicitly (`NO PRICE ENTRY`) for any row with
real tokens and no match.

## Timestamps and crash recovery

- Rows are backdated to the producer's own `ts`, parsed to naive UTC (`_parse_ts`, `:72-82`), so a
  2am run's spend lands on the right calendar day even though ingestion trails by up to a few
  minutes.
- A leftover `usage.jsonl.ingest` file means a prior ingest crashed mid-commit — it's retried
  automatically on the next cycle (`:85-92`, deletion happens only after a successful commit). This
  is **not** a stuck run.
- Fewer usage lines than `Synthesis complete` log lines for a given night is a metering gap, not
  missing work — `_record_usage` in the organizer is deliberately best-effort and never blocks
  synthesis on a write failure.

## Fast triage

- Ratio ~0.5 across the board → the discount landed.
- Ratio ~1.0 → batch fell back to sync for that request — cross-reference `nexus-brain-batch-debug`
  for why.
- `anthropic/`-prefixed models → OpenRouter fallback, priced by the stripped name.
- Real tokens with `cost_usd == 0.0` → a price-table gap, not a free night.
- `usage.jsonl.ingest` present on disk → a prior crash, self-healing, leave it.
