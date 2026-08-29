#!/usr/bin/env bash
# PostToolUse: ruff-check a backend .py file just edited. Advisory only — the
# edit already happened, this can't block it, only surface findings.
set -euo pipefail

input=$(cat)
file=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' <<<"$input" 2>/dev/null || true)

[[ "$file" == */backend/*.py ]] || exit 0

ruff_bin="/opt/nexus/venv/bin/ruff"
[[ -x "$ruff_bin" ]] || ruff_bin=$(command -v ruff || true)
[[ -n "$ruff_bin" ]] || exit 0

"$ruff_bin" check --no-cache "$file" 1>&2
