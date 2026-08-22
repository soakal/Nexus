---
name: nexus-chat-lanes
description: Chat is Haiku-routed intent lanes (CHAT/MAIL/CALENDAR/VAULT/...), not one loop — and the CHAT lane specifically is single-shot streaming that cannot carry custom tools, so "just give chat a tool" doesn't work for CHAT the way it does for the other lanes. Covers the classifier, the run_with_tools consumers, and the three vault path namespaces title resolution has to bridge. Use before adding a chat capability, when a chat answer seems to ignore an available tool, or when a vault path/title lookup behaves unexpectedly.
---

# How NEXUS chat actually routes

`backend/agents/chat.py::chat()` is not one conversational loop — every message is
Haiku-classified into an intent (`HOME_CONTROL | TASK | CHAT | NOTE | STATUS | MAIL |
MAIL_SEND | CALENDAR | VAULT`, the classifier prompt around `chat.py:425-441`), then
dispatched to a dedicated `elif intent == "X":` branch. Getting this wrong was a real
mistake made mid-project: a feature request assumed CHAT already ran a multi-step
tool loop with `vault_search`. It doesn't, and the actual architecture below is why.

## The CHAT lane specifically cannot carry custom tools

CHAT's reply path is `sonnet(...)` / `stream_sonnet(...)` (`chat.py:507` onward),
which bottoms out in `router._create_streaming_sync` (`router.py:1228-1245`) or the
non-streaming equivalent. Both only ever conditionally add Anthropic's **hosted**
`_WEB_SEARCH_TOOL` (`router.py:501`, added at `:1238` when `web_search=True`) — there
is no code path for passing custom `tool_specs`/`dispatch` into either. A client-side
tool like `vault_search` or `vault_read_note` is structurally unreachable from CHAT.

**If a chat capability needs a custom tool, it needs its own intent lane**, not a
change to CHAT. Converting CHAT itself to a tool loop would kill token streaming for
every message, not just the ones that need a tool — MAIL (`chat.py:879-962`), CALENDAR
(`chat.py:825-877`), and VAULT are all separate lanes for exactly this reason. A
misrouted question just falls through to CHAT's own (narrower) handling — for vault
questions specifically, `memory.vault_recall`'s snippet injection into
`[VAULT NOTES]` (`backend/agents/memory.py:10-11,42-43,111-113`, capped at 800 chars
total) is the CHAT-lane fallback, not a second implementation of the VAULT lane.

## `run_with_tools` is the multi-step machinery, and it already existed

`router.run_with_tools` (`router.py:1093-1225`) is a real multi-round loop (max 5
rounds, `_loop_guard`'s BUDGET→KILL→CANCEL brake before every round, a moving cache
breakpoint) — chat just never had a lane routed into it before VAULT. Its other
callers, for reference when adding a new one:

- `orchestrator.py:195` (`orchestrator_execute`) and `:310` (`orchestrator_verify`) —
  the durable task executor/verifier, using either the plain read-only registry
  (`tools.tool_specs()`/`dispatcher_map()`) or, when `agent_write_enabled` is on
  (`orchestrator.py:103,151`), the larger write-capable set from
  `write_tools.all_tool_specs()`/`all_dispatchers()`.
- `incident_diag.py:96` — a deliberate one-shot call, explicitly NOT a durable Task
  (see that file's own docstring for why).
- `chat.py`'s VAULT branch — the newest caller, scoped to exactly two tools
  (`vault_search`, `vault_read_note`) rather than the full registry, so a vault
  question can't wander into unrelated tool calls.

Adding a new lane that needs tools = add the intent to the classifier prompt
(`chat.py:425-441`) and the whitelist (`chat.py:467`), add an `elif intent == "X":`
branch that calls `run_with_tools` with a scoped tool subset, and — if it's a
conversational (not imperative/status) intent — add it to the fact-extraction gate
(`chat.py:1042`, currently `("CHAT", "TASK", "NOTE", "VAULT")`).

## Three vault path namespaces, and where title resolution lives

A note can be named three different ways depending on which code touched it last:

| Namespace | Shape | Where it comes from |
|---|---|---|
| Vault-root-relative | `Brain/wiki/X.md` | `vault_search` results (`obsidian.py`'s `md_file.relative_to(vault)`) |
| Brain-relative | `wiki/X.md` | `/api/vault/note`, `read_note_text`, `vault_read_note`'s internal path attempts |
| Wiki-basename | `X.md` | The catalog's `filename` field (`_meta/wiki-catalog.json`) |

`backend/agents/tools.py::_vault_read_note` bridges these: strips a leading `Brain/`,
appends `.md` if missing, then tries a direct `read_note_text` read before falling
back to title resolution.

Title/stem resolution to a real path exists in **three places**, all deliberately
kept in the same rung order (exact stem → exact title → case-insensitive stem →
case-insensitive title, mirroring `modules/brain-organizer/brain_organizer.py`'s
`_defuse_unknown_wikilinks`), but with different **ambiguity semantics** — this is
the trap:

1. `brain_organizer._defuse_unknown_wikilinks` — the nightly writer, no ambiguity
   concept, has a 5th fuzzy rung, writes to disk.
2. `backend/api/vault.py`'s `_build_graph_sync`'s inline `resolve()` — feeds the
   graph view. First-wins via `setdefault` maps: a genuine tie is silently resolved
   to whichever page happened to populate the map first. Harmless here — a
   plausible-but-wrong graph edge is just noise.
3. `backend/api/vault.py::resolve_note_candidates` — feeds `vault_read_note`.
   Returns **every** match at the winning rung, never picks one, because a silent
   wrong pick here is a wrong fact told to the user, not noise. The tool's dispatch
   turns 2+ candidates into an "Ambiguous — ... Ask the user which one" string
   instead of guessing.

None of the three has the fuzzy rung — read-time fuzzy matching fabricates a
match; the nightly writer already resolved every fuzzy-resolvable link into
filename-stem space on disk, so anything still unresolved at read time is a
genuinely broken link, not a near miss worth guessing at.

## Fast triage

- Chat ignored an available tool / gave a snippet instead of a full answer → check
  what intent it actually classified as (`chat.py:473`'s log line) before assuming
  the tool is broken — a misroute to CHAT never had the tool available at all.
- "Read my note on X" did something unexpected → check which of the three path
  namespaces X actually is, and which rung of `resolve_note_candidates` it should
  have hit; an ambiguous title returns a candidate list, not a guess.
- Adding a new tool-using chat capability → it needs its own lane (see above), it
  cannot be bolted onto CHAT.
