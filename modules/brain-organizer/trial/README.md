# Brain Organizer trial (Trial B) — 2026-08 OpenRouter model-swap trial

Tests whether `google/gemini-2.5-pro` can replace `claude-sonnet-4-6` for the
nightly Brain Organizer wiki-synthesis run, without touching production.

## How it works

Every night, two cron jobs on nexus-lxc:

1. **01:55 `snapshot`** — copies that night's real `raw`/`wiki`/`_meta`/`_Index.md`
   (NOT `.stversions`, that's 591MB of Syncthing history) into an isolated
   `/var/lib/nexus/brain-trial/vault/Brain/` tree, before the real 02:00 run
   consumes and deletes the raw files. Also freezes a separate, untouched copy
   of `wiki/` to `/var/lib/nexus/brain-trial/wiki-baseline/` — the trial's
   own working copy (`vault/Brain/wiki`) gets mutated in place by the run
   step, so this baseline is what makes an apples-to-apples diff possible.
2. **03:00 `run`** — copies `brain_organizer_trial.py` (this repo, a deliberate
   fork of `brain_organizer.py` — see that file's own docstring for the two
   real changes) into an isolated module directory, then runs it against the
   snapshot with a trial `config.json` pointing `sonnet_model`/`haiku_model`
   at OpenRouter model ids. Afterward: `diff-trial.txt` (the trial's own
   delta against `wiki-baseline` — the clean signal, isolated from anything
   the live real vault picked up in the meantime), `diff-real.txt` (the real
   vault's delta against the same baseline, for reference only — it can
   still include unrelated background writes, since the real vault stays
   live the whole time), and a `wikilink_census.py --before <real> --after
   <trial>` run for an objective broken-link-count comparison.

   Earlier builds diffed the trial's output straight against the live real
   vault, which mixed in every unrelated write the real vault picked up
   between snapshot and diff time (goal completions, digest entries, etc.) —
   found live on 2026-08-16 when a manually-triggered test run 7 hours after
   the real run produced a 3500-line diff that was almost entirely noise.
   `diff-trial.txt`/`wiki-baseline` fix that.

Nothing in `/var/lib/nexus/knowledge/Brain` (the real vault) or the real
`brain_organizer.py`/`config.json` is ever touched. The real 02:00 run is
completely unaffected — it runs on its own schedule, own directory, own lock
file, own usage log.

## Setup (one-time, on nexus-lxc)

```bash
mkdir -p /var/lib/nexus/brain-trial/{vault,module,nights}
cp /opt/nexus/modules/brain-organizer/trial/run-trial.sh /var/lib/nexus/brain-trial/run-trial.sh
chmod +x /var/lib/nexus/brain-trial/run-trial.sh
cp /opt/nexus/modules/brain-organizer/config.json /var/lib/nexus/brain-trial/config.json
# then edit /var/lib/nexus/brain-trial/config.json per config.template.json in this dir
crontab -l | { cat; echo "55 1 * * * /var/lib/nexus/brain-trial/run-trial.sh snapshot >> /var/lib/nexus/brain-trial/cron.log 2>&1"; echo "0 3 * * * /var/lib/nexus/brain-trial/run-trial.sh run >> /var/lib/nexus/brain-trial/cron.log 2>&1"; } | crontab -
```

## Teardown

```bash
crontab -l | grep -v brain-trial | crontab -
rm -rf /var/lib/nexus/brain-trial
```

Nothing to revert in the real repo or the real deployment — `brain_organizer_trial.py`
can stay checked in indefinitely as a harmless, never-imported, never-run-in-place file,
or be deleted once the trial's decided.

## Files

- `run-trial.sh` — the two-mode (`snapshot` | `run`) runner, deployed as-is to nexus-lxc.
- `config.template.json` — the exact key diff from the live `config.json`; not the
  live config itself (that's gitignored, environment-specific, and lives only on
  the box).

Powered by CwiAI

## Luna trial (2026-09, prepared — crons NOT installed until Brian says go)

Same harness, new candidate: `openai/gpt-5.6-luna` for both `sonnet_model` and
`haiku_model` (`api_provider: openrouter`; the trial fork routes any `/`-bearing
model id straight to OpenRouter regardless, `reasoning_max_tokens: 2048` maps to
a low/medium reasoning budget there). Sync price, no batch — quality is what's
being measured. Expected cost ≈ $0.35 for 5 nights.

**Go:** install the two crons from *Setup* above (snapshot 01:55 + run 03:00).
**Stop:** `crontab -l | grep -v brain-trial | crontab -` — nothing else to undo.

**Each morning (5 nights):** read `nights/<date>/rc` (0 = ran), skim
`diff-trial.txt` and `census.md`, then append one line to
`/var/lib/nexus/brain-trial/preference-log.md`:

```
YYYY-MM-DD | preferred|indifferent|worse | one-line why
```

**Pass bar (from NEXUS-PROVIDER-COMPARE):** 0 failed nights (`rc` = 0 on 5/5),
wikilink census not worse on ≥4/5, `preferred`/`indifferent` on ≥3/5, output
tokens within ±20 % of the Sonnet run (`nights/<date>/usage.jsonl`). Anything
short of that is a NO-GO and the crons come out; nothing in production changed
either way.
