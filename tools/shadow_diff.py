"""Summarize backend/agents/router.py's Trial A shadow log
(/var/lib/nexus/logs/shadow.jsonl) -- read-only, stdlib only, no backend
imports (same discipline as tools/cleanup_calibration_contamination.py).

Per label: call count, agreement rate, and a directional breakdown for the
two one-word classifiers (mail_junk_classify, mail_reply_classify,
action_judge) whose disagreements can be judged as "harmful" (the shadow
model would have let something through, or trashed something, the real
model didn't) vs "safe" (the reverse). Free-form JSON-emitting labels
(facts_extract, goal_proposer) are compared on JSON-parseability + top-level
key set, not text equality -- exact text match on free-form generation would
be a meaningless near-0% "agreement" rate.

Usage:
    python tools/shadow_diff.py [PATH]   # defaults to /var/lib/nexus/logs/shadow.jsonl

Powered by CwiAI
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

_DEFAULT_PATH = "/var/lib/nexus/logs/shadow.jsonl"

# label -> (real_verdict, shadow_verdict) -> "harmful" | "safe" | None (no verdict pair to judge)
_HARMFUL_DIRECTION = {
    "mail_junk_classify": lambda a, b: a.strip().upper() == "KEEP" and b.strip().upper() == "JUNK",
    "mail_reply_classify": lambda a, b: a.strip().upper() == "NO" and b.strip().upper() == "YES",
    "action_judge": lambda a, b: "veto" in a.lower() and "veto" not in b.lower(),
}

_JSON_LABELS = {"facts_extract", "goal_proposer"}

# Same fence-stripping as backend/agents/trial_report.py's _parseable --
# duplicated on purpose (tools/ never imports backend/), keep in sync by hand.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def _parseable(text: str) -> bool:
    text = text.strip()
    m = _FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    try:
        json.loads(candidate)
        return True
    except Exception:
        return False


def _load_rows(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _is_harmful(label: str, out_a: str, out_b: str) -> bool:
    check = _HARMFUL_DIRECTION.get(label)
    if check is None:
        return False
    try:
        return bool(check(out_a, out_b))
    except Exception:
        return False


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_PATH)
    rows = _load_rows(path)
    if not rows:
        print(f"No shadow rows found at {path}")
        return 0

    by_label: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_label[r.get("label", "")].append(r)

    print(f"{len(rows)} shadow calls across {len(by_label)} labels ({path})\n")

    disagreements = []
    for label, label_rows in sorted(by_label.items()):
        n = len(label_rows)
        if label in _JSON_LABELS:
            parseable = sum(1 for r in label_rows if _parseable(r.get("out_b", "")))
            print(f"{label:24s} n={n:4d}  shadow JSON-parseable: {parseable}/{n} ({parseable/n*100:.0f}%)")
            continue

        n_dis = sum(1 for r in label_rows if not r.get("agree", True))
        n_harmful = sum(
            1 for r in label_rows
            if not r.get("agree", True) and _is_harmful(label, r.get("out_a", ""), r.get("out_b", ""))
        )
        pct = n_dis / n * 100 if n else 0.0
        flag = f"  <-- {n_harmful} HARMFUL" if n_harmful else ""
        print(f"{label:24s} n={n:4d}  disagreed={n_dis:3d} ({pct:.1f}%){flag}")

        for r in label_rows:
            if not r.get("agree", True):
                disagreements.append((label, r, _is_harmful(label, r.get("out_a", ""), r.get("out_b", ""))))

    if disagreements:
        print(f"\n{len(disagreements)} disagreement(s) — harmful-direction ones first:\n")
        disagreements.sort(key=lambda t: not t[2])  # harmful (True) first
        for label, r, harmful in disagreements:
            tag = "HARMFUL" if harmful else "safe"
            print(f"[{tag}] {label} @ {r.get('ts', '?')}")
            print(f"  prompt: {r.get('prompt', '')[:200]!r}")
            print(f"  real ({r.get('model_a')}):   {r.get('out_a', '')[:150]!r}")
            print(f"  shadow ({r.get('model_b')}): {r.get('out_b', '')[:150]!r}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
