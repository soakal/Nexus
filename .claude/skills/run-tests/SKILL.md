---
name: run-tests
description: Run the nexus test suite (plus ruff/mypy) the right way for wherever you're editing — devbox has no venv, nexus-lxc does. Use whenever you want real test confirmation after editing nexus code, before claiming something works.
---

# Running nexus's tests

Decide first: has the change already been pushed and pulled onto `/opt/nexus` on nexus-lxc?

- **Yes** — run in place:
  ```bash
  ssh -i ~/.ssh/id_ed25519 root@100.84.21.43 'cd /opt/nexus && venv/bin/pytest tests/ -q 2>&1 | tail -30'
  ```
- **No** (still local/uncommitted on devbox) — follow
  [`nexus-test-without-venv`](../nexus-test-without-venv/SKILL.md) exactly: bundle your real HEAD,
  scp it over, clone into a throwaway dir on nexus-lxc, test with `/opt/nexus/venv/bin/pytest`
  there. Read that skill first — the clone-from-`/opt/nexus` trap is easy to hit.

## Lint and types

In the same remote checkout (throwaway clone or `/opt/nexus`), run:

```bash
venv/bin/ruff check backend/ && venv/bin/mypy backend/
```

devbox has neither installed system-wide — don't try to run these locally.

## Reading the result

Compare failures against the known baseline, not zero — check `nexus-test-without-venv`'s dated
baseline entry and CLAUDE.md's own dated pytest-run entries for the current count before treating
any failure as a regression from your change.
