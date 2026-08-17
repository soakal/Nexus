---
name: nexus-test-without-venv
description: Run nexus's real pytest suite against a local (devbox) change before committing/pushing — devbox has no nexus venv (heavy deps: torch/whisper/fastapi/sqlmodel), only nexus-lxc does. Use whenever you've edited nexus code on devbox and want real test confirmation rather than trusting a syntax check alone.
---

# Testing a nexus change without a local venv

devbox is where the nexus repo is edited, but it has no `venv/` of its own — `pytest`,
`fastapi`, etc. aren't installed system-wide, and building a full venv means installing
`requirements.txt`'s heavy deps (torch/whisper included) just to run a test. nexus-lxc already
has a working venv (`/opt/nexus/venv`) — reuse it against a throwaway clone instead.

## The gotcha: don't clone from `/opt/nexus`

`/opt/nexus` on nexus-lxc is the deployed checkout — it's often BEHIND your local devbox commits
(anything not yet pushed+pulled). Cloning from it and then `git apply`-ing a diff built against
your local HEAD fails with `patch does not apply` / `patch failed: <file>:1` if any touched file
differs between the two. This is a silent trap, not an obvious error — the failure looks like a
bad patch when it's really a stale base.

## The actual steps

1. **Bundle your real local HEAD** (not `/opt/nexus`), including any staged-but-uncommitted diff
   you want to test:
   ```bash
   cd ~/repos/nexus
   git bundle create /tmp/nexus.bundle HEAD
   git diff --cached > /tmp/mychange.patch   # only if testing uncommitted staged changes
   ```
2. **Ship both to nexus-lxc**:
   ```bash
   scp -i ~/.ssh/id_ed25519 /tmp/nexus.bundle root@100.84.21.43:/tmp/nexus.bundle
   scp -i ~/.ssh/id_ed25519 /tmp/mychange.patch root@100.84.21.43:/tmp/mychange.patch  # if applicable
   ```
3. **Clone the bundle into a throwaway dir and test with the real venv**:
   ```bash
   ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 '
   rm -rf /tmp/nexus-test && git clone -q /tmp/nexus.bundle /tmp/nexus-test && cd /tmp/nexus-test &&
   git apply /tmp/mychange.patch &&   # skip this line if you bundled a commit, not a staged diff
   /opt/nexus/venv/bin/pytest tests/ -q 2>&1 | tail -30
   '
   ```
4. **Clean up** both ends when done — `rm -rf /tmp/nexus-test /tmp/nexus.bundle /tmp/mychange.patch`
   on nexus-lxc, and the local `/tmp` copies.

## Reading the result

Compare against the known pre-existing baseline (as of 2026-08-16): 4 flaky/pre-existing
failures unrelated to any real change — two hardcoded scheduler-job-count-29-vs-28 tests in
`tests/test_coverage_boost.py`, and `tests/test_proposer.py::test_known_hardware_issue_light_goal_dropped`
(time-of-day-flaky). Anything beyond that baseline is a real regression from your change. Check
`CLAUDE.md`'s own dated entries for whether that baseline count has since changed.

## When this doesn't apply

If you're testing a change already pushed to `origin/main` and pulled onto nexus-lxc's real
`/opt/nexus`, just run `/opt/nexus/venv/bin/pytest tests/` there directly — no bundle/clone
needed, you're already testing the real deployed checkout.
