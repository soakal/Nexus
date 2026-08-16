# Brain Organizer trial (Trial B) — 2026-08 OpenRouter model-swap trial

Tests whether `google/gemini-2.5-pro` can replace `claude-sonnet-4-6` for the
nightly Brain Organizer wiki-synthesis run, without touching production.

## How it works

Every night, two cron jobs on nexus-lxc:

1. **01:55 `snapshot`** — copies that night's real `raw`/`wiki`/`_meta`/`_Index.md`
   (NOT `.stversions`, that's 591MB of Syncthing history) into an isolated
   `/var/lib/nexus/brain-trial/vault/Brain/` tree, before the real 02:00 run
   consumes and deletes the raw files.
2. **03:00 `run`** — copies `brain_organizer_trial.py` (this repo, a deliberate
   fork of `brain_organizer.py` — see that file's own docstring for the two
   real changes) into an isolated module directory, then runs it against the
   snapshot with a trial `config.json` pointing `sonnet_model`/`haiku_model`
   at OpenRouter model ids. Afterward: a `diff -ru` against the real wiki, and
   a `wikilink_census.py --before <real> --after <trial>` run for an objective
   broken-link-count comparison.

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
