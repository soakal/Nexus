# Daily Claude Features Digest — Instructions

You are producing a daily digest of NEW Claude/Anthropic capabilities Brian could adopt -- things he is NOT already doing. Brian is a sophisticated, heavy Claude Code power user. Do NOT suggest anything already in the list below; the whole point of this digest is to surface things outside his existing process.

Already in active use -- never re-suggest these or close variants:
- Claude Code CLI as primary dev tool: custom slash commands, subagents, skills, hooks, a persistent file-based memory system, the Agent tool and Workflow tool (pipeline/parallel multi-agent orchestration), scheduled cloud routines (this digest IS one).
- Council Loop: a from-scratch three-role autonomous coding loop (Arbiter/Opus plans, Engineer/Sonnet implements, Realist/Sonnet reviews, auto-commits on accept), driven via /loop, with its own doctor/repair/rollback commands and config-schema validation.
- SOPForge: a self-hosted Scribe alternative with its own autonomous planner/executor loop (Claude Fable 5 planner+reviewer, Claude Sonnet 5 executor), local-first with an Ollama runtime fallback.
- NEXUS: a personal software project with a backend, frontend, several integrations, spending limits, and a review/approval layer for automated actions.
- ponytail mode, a custom lazy-coding-discipline skill/workflow.
- Custom MCP servers (an Obsidian vault server, Chrome browser automation) plus Gmail/Calendar connectors.
- Already on the latest model lineup: Opus 4.8, Sonnet 5, Haiku 4.5, Fable 5.

Your job each run: check what's genuinely new from Anthropic since your last check (look back ~48 hours; if this looks like the first run ever, look back ~7 days instead) -- new Claude models, new Claude Code CLI features/flags/settings/skills-ecosystem changes, new Agent SDK capabilities, new Claude API features (tool use, new endpoints, context window, computer use, MCP protocol, pricing).

Tool-call budget: make at most one WebFetch call total, to https://docs.claude.com/en/release-notes/claude-code (the single most relevant page). For anything beyond that page, use WebSearch instead of WebFetch -- at most 2-3 targeted queries. Do not rely on training data, it's stale, but stay within this call budget.

Filter hard: only include items that are (a) actually new/changed in the lookback window, and (b) not already something Brian does per the list above. If nothing new and relevant turns up, say so plainly in one line -- do not pad with filler, generic tips, or restating things from the already-doing list. An empty or near-empty digest on a quiet day is the CORRECT output, not a failure.

For each item that survives the filter: what it is (1-2 sentences), why it specifically matters for NEXUS, SOPForge, or Council Loop (or his general Claude Code workflow) where a real connection exists -- don't force a tie-in if there isn't one, and a concrete first step to try it.

This repository's default branch is master, NOT main. Output: in this repo, create/commit a new markdown file at digests/claude-features/YYYY-MM-DD.md (use today's actual date) containing the digest, then push directly to the master branch. Keep the file tight: a one-line date header, then 0-5 items, each 3-5 lines. Do not modify any other file in the repo (except this file may be updated if these instructions need to evolve). Do not open a PR and do not create a new branch -- commit and push straight to the existing master branch on origin.
