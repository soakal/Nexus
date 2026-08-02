# Brain Organizer — Robustness & Integration-Readiness Spec v1

**Role:** Opus planner. **Next role:** Sonnet writer.
**Repo:** `C:\Users\Brian\Documents\Agentic os\nexus`
**Companion spec:** `docs/brain-organizer-wikilink-router-fixes-spec.md` (the two named bugs). This spec does **not** duplicate it — it addresses the *class* of defect, and several findings below are prerequisites or successors to it.
**Ordering constraint:** §1 and §2 are live-bug fixes and should ship **before** the companion spec, because they are cheaper and one of them (§1) is actively corrupting topic pages every night.

---

## 0. Summary — what I actually found

Everything below is measured against the live vault (`C:\Users\Brian\iCloudDrive\iCloud~md~obsidian\Brain\`, 269 wiki pages, 369-record ledger, 195 backups) or the real repo, not inferred.

| # | Finding | Site | Status | Severity |
|---|---|---|---|---|
| **F1** | The daily-note guard keys on a **retired emitter's filename prefix** (`event-hermes-`). NEXUS took over that emission on 2026-07-27 as `event-nexus-…`; the guard was never updated. | `brain_organizer.py:632` | **Live, 6 nights, ongoing** | High |
| **F2** | The ledger is keyed by content SHA. The empty-file SHA has been recorded as a *success* since 2026-06-19, so every zero-byte raw note is permanently, silently invisible. Two real Quill captures have sat in `raw/` since 2026-07-30 with no error, no Telegram, no ledger row. | `:97`, `:192`, `:1473` | **Live** | High |
| **F3** | Two wikilink normalizers in the same directory targeting **opposite namespaces**: the inbound MCP path canonicalizes to filename *stems*, the outbound synthesis path validates against *titles*. The inbound path creates exactly the form the outbound path destroys. | `mcp_server.py:118` vs `brain_organizer.py:863` | Structural | High |
| **F4** | `catalog_summary_chars` is honored on one code path and ignored on the other — the same defect shape the companion spec found in `new_page_similarity_threshold`. `api_provider` is read nowhere. Three config keys have code defaults that differ from `config.json`. Nothing validates the file. | `:326/:355` vs `:1261`; `config.json:22` | Latent | Medium |
| **F5** | The catalog layer — the single data structure every downstream step reads — has **zero direct tests**. So do `find_similar_page`, `_normalize_title`, `load_config`. The one 5b test patches `re.split` to raise, so the splice logic itself is never executed by any test. | `tests/` | Coverage shape | Medium |
| **F6** | A run that succeeds at everything and is wrong at everything is byte-identical to a good run. Neither of the companion spec's bugs, nor F1, nor F2, would produce one anomalous line. `organizer.log` is 3.1 MB unbounded, and the dashboard reads all of it on the event loop. | `:1560`, `:81`, `backend/api/brain_organizer.py:73` | Observability | Medium |
| **F7** | Adding a new raw source requires editing three hardcoded sites buried mid-file, and a fourth source rule lives in a *different, dormant* module for a file shape that is in `raw/` right now. | `:604`, `:617`, `:636`; `wiki_ingest.py:58` | Extensibility | Medium |
| **F8** | `consolidate_wiki.py`'s copies have **already drifted behaviorally**, not just structurally. Six duplication sites total. | see §8 | Drift | Low–Medium |

---

## 1. F1 — The daily-note guard is keyed to a dead emitter (LIVE)

### 1.1 The code

`_is_daily_note` (`brain_organizer.py:617`–`633`), line `:632`:

```python
return bool(_DATE_IN_STEM_PAT.search(stem)) or stem.startswith("event-hermes-")
```

The `event-hermes-` prefix exists because Hermes's daily digest is timestamped `20260724T120009Z` — no hyphens — so `_DATE_IN_STEM_PAT` (`\d{4}-\d{2}-\d{2}`) misses it. The docstring at `:626` names that file explicitly.

**Hermes's Telegram/digest role was retired 2026-07-26 (CLAUDE.md, "Hermes link — Phase 1"). NEXUS now emits the same digest itself, as `event-nexus-nexus-daily-digest-<ts>.md`.** The guard was never updated.

### 1.2 Verified live

```
'event-hermes-hermes-daily-digest-20260724T120009Z'  -> _is_daily_note = True
'event-nexus-nexus-daily-digest-20260802T120508Z'    -> _is_daily_note = False
'event-council-loop-run-complete-20260802T070438Z'   -> _is_daily_note = False
```

`processed.json` confirms the switchover exactly:

| Filename | Routed to |
|---|---|
| `event-hermes-hermes-daily-digest-20260722…30T120009Z` (9 nights) | `Daily-Log` — every time |
| `event-nexus-nexus-daily-digest-20260727T134420Z` | `AdGuard`, `Channels DVR`, `Agentic OS` |
| `…20260728Z` | `AdGuard`, `Channels DVR`, `Brain` |
| `…20260729Z` | **`Uncategorized`** |
| `…20260730Z` / `…20260731Z` / `…20260801Z` | `AdGuard`, `Channels DVR`, `Agentic OS`/`Brain` |

### 1.3 Measured damage

`Brain/wiki/AdGuard.md` now carries a growing per-day changelog:

```
### 2026-07-27 — from nexus.daily-digest 2026-07-27T13:44:20Z
### 2026-07-28 — from nexus.daily-digest 2026-07-28T12:05:24Z
### 2026-07-30 — from nexus.daily-digest 2026-07-30T12:05:10Z
### 2026-07-31 — from nexus.daily-digest 2026-07-31T12:05:10Z
### 2026-08-01 — from nexus.daily-digest 2026-08-01T12:05:09Z
```

`Channels-DVR.md` is accumulating Unraid parity and array-capacity facts. `Daily-Log.md` has not been written since 2026-07-31 (the last Hermes digest). This is precisely the "one page per calendar day, forever" fragmentation `_daily_note_route`'s docstring (`:646`–`:657`) says it exists to prevent — relocated into three unrelated *topic* pages, which is worse, because it is not visibly a dated page and nobody will spot it.

### 1.4 Fix

The bug is the *shape* of the test, not the value of the constant. Do not add `event-nexus-` to the list — a third emitter (`event-council-loop-…`) already exists and a fourth will. Generalize off the source segment:

At `:601`, add:

```python
_EVENT_NOTE_PREFIX_PAT = re.compile(r"^event-[a-z0-9]+(?:-[a-z0-9]+)*?-", re.IGNORECASE)
```

At `:632`, replace `stem.startswith("event-hermes-")` with `bool(_EVENT_NOTE_PREFIX_PAT.match(stem))`.

This is safe because the `_DAILY_NOTE_NAME_PAT` gate at `:631` already ran — only a stem that *also* contains `daily` or `briefing` reaches this line. `event-nexus-goal-completed-…` and `event-council-loop-run-complete-…` are unaffected (verified against the live `raw/` listing). Any future emitter's daily digest routes correctly with zero code change.

**Mirror obligation:** `backend/agents/wiki_ingest.py:75`–`95` carries a hand-copied twin of this function (its docstring and `brain_organizer.py:618` cross-reference each other). Apply the identical change there and add the drift test in §8.3.

### 1.5 Acceptance criteria

1. `_is_daily_note("event-nexus-nexus-daily-digest-20260802T120508Z")` is `True`.
2. `_is_daily_note("event-hermes-hermes-daily-digest-20260724T120009Z")` is still `True` (regression guard).
3. `_is_daily_note("event-council-loop-run-complete-20260802T070438Z")` is `False` — no `daily`/`briefing` token.
4. `_is_daily_note("event-nexus-goal-completed-20260802T142558Z")` is `False`.
5. `_is_daily_note("Daily-Driver-Setup")` is still `False` (the existing hijack guard, `:626`).
6. All 8 existing `_is_daily_note`/`_daily_note_route` tests pass unchanged.
7. `_daily_note_route("event-nexus-nexus-daily-digest-20260802T120508Z", …)` resolves to `wiki/Daily-Log.md`, not `wiki/daily/`, because the stem carries no `\d{4}-\d{2}-\d{2}` (matches the Hermes precedent — verified: 9 of 9 Hermes digests landed on `Daily-Log`).

### 1.6 Out of scope

Retroactively extracting the 5 digest sections out of `AdGuard.md` and the Unraid facts out of `Channels-DVR.md`. That is manual content work, same category as the companion spec's §4.6. Flag it to Brian; the pipeline must never auto-un-merge two pages.

---

## 2. F2 — The SHA-keyed ledger silently swallows notes (LIVE)

### 2.1 The code

`scan_raw_folder` (`:191`–`:204`) computes `sha = compute_sha256(f)` and skips the file entirely when `processed[sha]` is a success record. `_record_success` (`:1473`) writes `processed[sha] = {"filename": …}` — the filename is **recorded but never consulted**. The ledger's identity is content, not file.

### 2.2 Verified live

```
empty-file sha in ledger? True
  {'filename': 'TODO - Work PC MCP Vault Setup 1.md',
   'timestamp': '2026-06-19T04:00:19.620451+00:00',
   'topics': ['Uncategorized']}
```

`Brain/raw/` right now:

```
0 bytes  2026-07-30 22:14  Quill-Jul 30 2026 at 10:14 PM.md
0 bytes  2026-07-30 22:54  Quill-Jul 30 2026 at 10:54 PM.md
```

`processed.json` has **369 records and zero failures**. Both files were captured by the Quill iOS Shortcut (documented live 2026-07-31 in `Brain.md:229`), have been scanned six consecutive nights, and produced no ledger row, no log line, no Telegram message. They are structurally unreachable and will sit there forever.

### 2.3 Why this matters for *future* integrations specifically

The failure requires only that two notes share a body. Two `event-nexus-goal-completed-…` files were emitted 11 seconds apart today (`142558Z` and `142609Z`) — their bodies differ by one goal id, but `emit_event`'s template (`obsidian.py::_format_event`) is fixed and short, so collision is a matter of when, not if. The outcome-tracker spec's Phase 2 `emit_event("flag.resolved", …)` idea (`outcome-tracker-spec.md:249`) would emit exactly this shape at higher volume. A Slack-export or voice-memo pipeline emitting repeated boilerplate is the same hazard.

### 2.4 Fix — two independent changes, both small

**(a) Reject empty content explicitly, at `scan_raw_folder`.** A zero-byte or whitespace-only file is not a note. Skip it, log it once at WARNING, and count it into the run summary (§6). Do **not** record it in the ledger and do **not** send a per-file Telegram — the two Quill files would otherwise page Brian nightly forever.

**(b) Make the ledger hit conditional on filename agreement.** In `scan_raw_folder`, a success record whose `record.get("filename")` differs from `f.name` is treated as a **new** file. Back-compatible with all 369 existing records; no ledger migration; one condition.

This is correct on the merits, not just as a workaround: the raw file is unlinked on success (`:1289`), so a file *still present* whose content-hash matches a *differently named* completed note is genuinely a distinct note that was never processed.

### 2.5 Acceptance criteria

8. A zero-byte `.md` in `raw/` is not returned by `scan_raw_folder`, writes one WARNING naming the file, and adds no key to `processed`.
9. A whitespace-only file behaves identically.
10. Given `processed = {sha_of("x"): {"filename": "a.md", "topics": [...]}}` and a file `b.md` containing `"x"`, `scan_raw_folder` **returns** `b.md`.
11. Given the same ledger and a file `a.md` containing `"x"`, `scan_raw_folder` does **not** return it (unchanged behavior).
12. A `failed`-status record still uses the existing `attempts`/`max_file_attempts` path regardless of filename (`:195`–`:203` untouched).
13. All 7 existing `scan_raw_folder` tests pass unchanged.

---

## 3. F3 — Two wikilink normalizers, opposite namespaces

### 3.1 The finding

`mcp_server.py` is not a thin HTTP shim over the module — it contains an independent, second implementation of wikilink handling, and it **already does the thing the companion spec is about to add to `brain_organizer`**:

| | inbound (`POST /raw`) | outbound (synthesis) |
|---|---|---|
| Regex | `mcp_server.py:40` — 3 capture groups, `(?<!\!)` embed guard | `brain_organizer.py:860` — 2 groups, non-capturing anchor, no embed guard |
| Normalization key | `_canonical_key` (`:78`) — lowercase, spaces→hyphens | `_normalize_title` (`:239`) — strips punctuation, **stems suffixes** |
| Target namespace | filename **stem** (`_build_stem_index`, `:84`) | page **title** (`known_titles`, `:884`) |
| Scope | Brain root + `wiki/` + `wiki/daily/` | `wiki/*.md` only |
| Unresolved | left intact + `## Broken Links` footer appended to the note body (`:147`) | rewritten to `` `backticks` `` |

The two disagree on all five axes. Concretely today: a note POSTed with `[[Mulch Needs]]` is rewritten inbound to `[[Mulch-Needs]]` (a real Brain-root file), then on the way out `_defuse_unknown_wikilinks` — whose catalog cannot see Brain root at all — finds no title `Mulch-Needs`, and `find_similar_page` stems it to `mulch need`, so the link is **backticked into plain text**. The inbound path manufactures exactly the link form the outbound path destroys.

The companion spec's §2.2 moves the outbound path into stem-space, which removes the *namespace* disagreement. It does not remove the *duplication*, the scope difference, or the second regex.

### 3.2 The `## Broken Links` footer is a second-order hazard

`_normalize_wikilinks` (`:147`–`:154`) appends a literal `## Broken Links` markdown section **into the note content that gets saved to `raw/`**. That content is then handed verbatim to Sonnet as "New information to integrate" (`:966`, `:1056`). Two consequences:

- In branch 5a the model is free to merge a diagnostic artifact into a real wiki page as if it were subject matter. One page already carries it: `wiki/2026-07-10-wikilink-triage-session-note.md`.
- In branch 5b the splice parser (`:995`) keys on `^## ` headers. A source note containing `## Broken Links` invites the model to echo that header back, and the splicer will then create or overwrite a `## Broken Links` section on a real topic page.

A diagnostic must not be indistinguishable from content.

### 3.3 Fix

**(a) One resolver, one owner.** After the companion spec lands, extract the canonical target-space index into `brain_organizer` as a public function and have `mcp_server` import it. The import direction is already established and cycle-free — `mcp_server.py:33` is `from brain_organizer import sanitize_topic_name`. Delete `mcp_server`'s `_canonical_key`/`_build_stem_index`/`_WIKILINK_RE`; keep `_normalize_wikilinks` as the HTTP-side wrapper that calls the shared resolver.

**(b) Reconcile scope.** The inbound index covers Brain root + `wiki/` + `wiki/daily/` for a reason documented at `mcp_server.py:85`–`:99`. The outbound catalog covers `wiki/*.md` only, also for a documented reason (`brain_organizer.py:646`–`:657`, and companion spec §7.2). These are both right for their own purpose, so **do not merge the scopes** — instead have the shared resolver take an explicit list of folders, and add a test that pins each caller's list. That makes the asymmetry deliberate and visible rather than emergent.

**(c) Move the broken-links report out of the note body.** Return it in the JSON response (`{"status":"ok","file":…,"broken_links":[…]}`) and, if a durable record is wanted, write an HTML comment (`<!-- broken-links: … -->`) instead of a `## ` heading. Callers today ignore the response body (`obsidian._post_raw` only checks `raise_for_status()`), so this is non-breaking.

### 3.4 Acceptance criteria

14. A link target that `mcp_server`'s inbound normalizer resolves to a stem is **not** backticked by `_defuse_unknown_wikilinks` for a page in `wiki/` — one test that runs both functions over the same input, asserting round-trip stability. This is the test that would have caught the namespace split.
15. `mcp_server.py` contains no `re.compile` for wikilinks and no `_canonical_key`; both come from `brain_organizer`.
16. The two callers' folder scopes are asserted explicitly (inbound: 3 folders; outbound: `wiki/` only), so widening either is a deliberate test edit.
17. `POST /raw` with an unresolvable `[[Nonexistent]]` returns `201` with `broken_links: ["Nonexistent"]` in the body and writes a file whose content contains **no** `## Broken Links` heading.
18. All existing `tests/test_mcp_server.py` tests pass, except the broken-links-footer assertions, which change intentionally.

---

## 4. F4 — Config is unvalidated, and one key is already half-ignored

### 4.1 `catalog_summary_chars` — the same defect as `new_page_similarity_threshold`

`build_wiki_catalog(wiki_folder, meta_folder)` (`:326`) takes no `config`. At `:355` it calls `_extract_page_entry(f)` — default `summary_chars=300`. But `process_file`'s in-run refresh (`:1260`) calls `_extract_page_entry(wiki_file, config.get("catalog_summary_chars", 300))`.

Setting `catalog_summary_chars` to anything but `300` produces a catalog where a page's summary length depends on **whether that page happened to be written during the current run** — and the mtime cache (`:352`) persists both variants into `wiki-catalog.json` indefinitely. The companion spec found the same shape once; this confirms it is a pattern, not an incident.

### 4.2 Dead and divergent keys

- **`api_provider`** (`config.json:22`, mirrored into `tests/conftest.py:69`) is read by **no code in the repo**. It reads like a live provider switch; it is inert. Anyone adding an OpenRouter-primary mode would reasonably assume it works.
- **Code default ≠ config value**, three times: `large_page_threshold_chars` (code `35000` at `:924`, config `20000`), `sonnet_max_tokens` (code `8192` at `:971`/`:1105`, config `16384`), `max_file_attempts` (code `5` at `:176`, config `2`, conftest `5`). Because `tests/conftest.py:54`–`:72` omits the first two entirely, **every synthesis test runs against a 35000-char threshold and an 8192-token cap that production never uses.**
- A typo'd key (`catalog_max_pages_in_promt`) silently no-ops the feature it names, forever, with no log line.

### 4.3 Fix

Add one module-level dict and one function, stdlib only:

```python
_CONFIG_DEFAULTS: dict[str, Any] = { ... }   # every optional key, one place
_CONFIG_REQUIRED: tuple[str, ...] = ("vault_path", "raw_folder", "wiki_folder",
                                     "meta_folder", "backup_folder", "logs_folder",
                                     "processed_file", "haiku_model", "sonnet_model")

def validate_config(config: dict[str, Any]) -> dict[str, Any]: ...
```

Behavior:
- Missing required key → **raise** with the key name. A pipeline that cannot find the vault must not run.
- Unknown key → **log WARNING** naming it (catches typos *and* surfaces `api_provider`). Never raise — Brian must be able to hand-annotate the file.
- Wrong type or out-of-range numeric → raise.
- Fill every absent optional key from `_CONFIG_DEFAULTS`, then return the merged dict.

Then: replace every inline `config.get(k, <literal>)` in `brain_organizer.py` with `config[k]` (11 sites), so a default can only be changed in one place; thread `summary_chars` into `build_wiki_catalog`; call `validate_config` at the top of `run()` (`:1378`) and `mcp_server.create_app()` (`:167`); and build `tests/conftest.py`'s `tmp_config` from `_CONFIG_DEFAULTS` plus the path overrides, so a test can never silently exercise a different threshold than production.

Delete `api_provider` from `config.json` and `conftest.py`, or wire it — Brian's call, but not both states.

### 4.4 Acceptance criteria

19. `validate_config({})` raises, and the message names `vault_path`.
20. `validate_config({**valid, "catalog_max_pages_in_promt": 60})` returns normally and logs one WARNING containing the misspelled key.
21. `validate_config({**valid, "sonnet_max_tokens": "8192"})` raises (type).
22. `validate_config({**valid, "new_page_similarity_threshold": 1.7})` raises (range).
23. A config omitting `large_page_threshold_chars` comes back with `_CONFIG_DEFAULTS["large_page_threshold_chars"]`, and that value equals the one in the real `config.json`.
24. A drift test asserts every key in the real `modules/brain-organizer/config.json` is in `_CONFIG_DEFAULTS ∪ _CONFIG_REQUIRED`, and vice versa for defaults — so adding a key to one without the other fails the suite.
25. `build_wiki_catalog` accepts `summary_chars` and a catalog built with `summary_chars=120` has every `summary` ≤ 120 chars.
26. `grep -c 'config.get("' brain_organizer.py` returns 0 for keys present in `_CONFIG_DEFAULTS`.
27. `tmp_config` in `conftest.py` is derived from `_CONFIG_DEFAULTS`; the full existing suite passes.

---

## 5. F5 — Test coverage shape: the catalog layer is untested

### 5.1 Measured

Grepping `tests/` for each function name:

| Function | Direct test |
|---|---|
| `build_wiki_catalog` | **none** — one mention, inside another test's docstring (`test_organizer.py:244`) |
| `_extract_page_entry` | **none** |
| `find_similar_page` | **none** — one mention, inside a `_defuse_…` test's *name* (`:398`) |
| `_normalize_title` | **none** |
| `load_config` | **none** |
| `route_topics` existing-title branch (`:785`–`:793`) | **none** |
| `route_topics` hallucination re-check (`:794`–`:799`) | **none** |
| 5b splice logic (`:992`–`:1025`) | **none** — the sole 5b test (`:849`) patches `re.split` to raise, so the splicer body never executes |

The catalog is the single data structure that feeds the router menu, the scope contract, the related-links block, and the wikilink validator. It has no tests, and it is exactly where the companion spec's defects B (sort-then-truncate, `:360`/`:707`) and C (BOM, `:270`) live. That is not a coincidence — it is the mechanism by which those bugs survived three weeks.

### 5.2 The untested branch that scares me most

`build_wiki_catalog`'s outer `try` (`:333`–`:381`) returns `[]` on any exception. With `catalog == []`:

- `catalog_block` is empty → Haiku is shown "EXISTING WIKI PAGES:" followed by nothing;
- `by_title` is empty → every `existing` route is logged as a hallucination and downgraded to `new` (`:794`–`:799`);
- `find_similar_page` over `[]` returns `None` → the near-dup guard cannot fire;
- every route creates a page.

**One transient exception during the catalog build turns a normal night into ~30 duplicate pages layered on top of the existing 269, and the run exits 0 and reports success.** There is no test and no assertion.

### 5.3 Required tests

**Catalog (new banner section):**

28. `_extract_page_entry` on a page with an H1, three `## ` headers and a lead paragraph returns the expected `title`/`headers`/`summary`.
29. `_extract_page_entry` respects `summary_chars`.
30. `_extract_page_entry` on a file with no H1 falls back to `f.stem`.
31. `build_wiki_catalog` over a 3-page tmp wiki returns 3 entries sorted by lowercased title, and writes `wiki-catalog.json`.
32. `build_wiki_catalog` reuses a cached entry when `st_mtime <= built_at` and re-parses when the file is touched. *(This test is also criterion 15 of the companion spec — write it once, it serves both.)*
33. `build_wiki_catalog` on a corrupt `wiki-catalog.json` falls back to a full rebuild without raising.
34. `build_wiki_catalog` never returns `[]` when `wiki_folder` contains readable `*.md` — and `run()` **aborts with a non-zero exit and a Telegram page** if it does (see §6.4). New behavior, not just a test.

**Similarity:**

35. `_normalize_title` parametrized over the three docstring examples plus an em-dash title.
36. `find_similar_page("Financial Forecasting", [Financial Forecast])` returns the entry; `find_similar_page("Kubernetes", [Financial Forecast])` returns `None`.
37. `find_similar_page` honors an explicit `threshold` argument (the companion spec's §2.2 depends on this being plumbed).

**Router resolution (the branch the companion spec's §4.5 rewrites):**

38. A `{"match":"existing","title":"<real catalog title>"}` response resolves to that page's `path_str` with `is_new=False`.
39. Two routes naming the same page de-duplicate to one result (`seen_paths`, `:789`).
40. A `{"match":"existing"}` naming a title not in the catalog logs the hallucination warning and falls through to the `new` path.

**5b splice — execute the real logic:**

41. An existing page containing `## Alpha` and `## Beta`, and a model response containing only a changed `## Beta`, produces a document where `## Alpha` is **byte-identical** and `## Beta` is replaced.
42. A response containing a `## Gamma` not present in the existing page appends it at the end and leaves everything before it byte-identical.
43. A response with leading preamble before the first `## ` discards the preamble (`:997`).
44. The existing `test_large_page_splice_failure_raises_instead_of_dropping_content` passes unchanged.

Criteria 41–43 are what the companion spec's defect-D fix needs in order to be provable at all.

---

## 6. F6 — Observability for a black-box nightly job

### 6.1 What exists

One Telegram summary (`:1560`–`:1571`): file count, comma-joined topic names, duration, and a failure count. Plus a per-file error message on failure (`:1503`). That is the entire signal surface.

It is guarded by `if success_count > 0:` — so **a run where every file fails sends N error messages and no summary, and a run that processes nothing sends nothing at all.** The two stuck Quill files (§2) are indistinguishable from an empty `raw/` folder every single night.

### 6.2 Would any of the known bugs have shown up?

| Bug | Signal in today's summary |
|---|---|
| Companion A (112 title-form links) | none |
| Companion B (78% of pages unroutable) | none — the summary prints topic *names*, and A/B/C names look normal |
| Companion D (43 large pages skip link processing) | none |
| **F1** (digest scattered into AdGuard) | none — "Topics updated: AdGuard, Channels DVR" reads correct |
| **F2** (two notes stuck 6 nights) | none |

Zero of five. Every one of them was found by a human audit weeks later.

### 6.3 Fix — five counters, no new machinery

Extend the existing summary. Each of these would have caught at least one real bug, and each is a few lines:

| Counter | Catches |
|---|---|
| `pages: N created / M merged` | Companion B — the creation rate never falling is the signature of an unreachable catalog |
| `links: N rewritten / M backticked` (returned from the normalizer) | Companion A and D — a large sustained backtick count is the bug |
| `catalog: N pages` (already computed at `:1403`, just not surfaced) | a degraded or empty catalog |
| `raw/ still holds N file(s)` after the run | F2 — would have fired on night one |
| `routes: {existing: N, new: M, uncategorized: K}` | the `Uncategorized` fallback (`:702`), which currently fires in total silence — it fired for the 2026-07-29 digest |

And: **send the summary unconditionally**, not only when `success_count > 0`. A nightly job that can be silently absent is a nightly job you stop trusting.

### 6.4 One hard assertion, not a heuristic

Per §5.2: if `build_wiki_catalog` returns `[]` while `wiki_folder.glob("*.md")` is non-empty, **abort the run before any synthesis**, exit non-zero, and page Telegram. There is no legitimate state in which the vault has 269 pages and the catalog has zero, and continuing costs ~30 duplicate pages that must be cleaned up by hand.

### 6.5 Log hygiene — and one event-loop violation

`setup_logging` (`:81`) uses a plain `FileHandler`. `organizer.log` is **3,166,144 bytes** and grows forever. `mcp_server.py:60`–`:64` already fixed the identical problem with a `RotatingFileHandler` after `mcp.log` reached 7 MB (`mcp.log.1` is still on disk at 7,051,727 bytes as proof). Apply the same one-line fix.

This is not merely tidiness. `backend/api/brain_organizer.py`'s `brain_organizer_status` does:

```python
lines = _LOG.read_text(encoding="utf-8").splitlines()
log_tail = [ln for ln in lines[-20:] if ...][-5:]
```

inside an `async def`, with no `asyncio.to_thread`, to obtain the last five lines. That reads 3.1 MB synchronously **on the event loop** on every dashboard poll, and grows without bound. CLAUDE.md's first hard-won rule is "Never block the asyncio event loop… Windows ProactorEventLoop + a blocked loop = `WinError 64`, dropped connections, 'everything offline'." Rotation caps it at 5 MB; wrapping the read in `asyncio.to_thread` (or seeking from the end) fixes it properly. Both are one line.

### 6.6 Acceptance criteria

45. The Telegram summary is sent even when `success_count == 0`, including on a zero-file run.
46. The summary contains `created=`, `merged=`, `links_rewritten=`, `links_backticked=`, `catalog=`, `raw_remaining=`, and `uncategorized=`.
47. `_defuse_unknown_wikilinks` (or its successor) returns counts alongside the text, and `synthesize_wiki` threads them up to `run()`. Existing callers that ignore the counts still work.
48. A run whose `build_wiki_catalog` returns `[]` while `wiki/` contains `*.md` returns a non-zero exit code, sends a high-priority Telegram, and performs **zero** writes to `wiki/` — provable by asserting `messages.create` was never called for a synthesis.
49. `setup_logging` installs a `RotatingFileHandler(maxBytes=5*1024*1024, backupCount=3)`.
50. `brain_organizer_status` does not call `read_text()` on the loop thread.

---

## 7. F7 — The extensibility seam that should exist

### 7.1 What adding a source costs today

There is exactly one decision point where a new raw source can be handled deterministically — `:1216`:

```python
routes = _daily_note_route(...) or route_topics(...)
```

Everything feeding it is hardcoded in three separate mid-file locations:

- `_looks_like_session_title` (`:604`) — three module-level regexes at `:597`–`:601`
- `_is_daily_note` (`:617`) — three more at `:587`–`:589`, plus the emitter prefix that F1 just proved wrong
- `_daily_note_route` (`:636`) — the destination map, inline, with `Daily-Log.md` as a bare literal at `:673`

And a **fourth** rule for a source that is in `raw/` right now lives in a different, dormant module: `wiki_ingest.py:53`–`:61` defines `_FEATURES_DIGEST_PAT` and `_DIGEST_PAGE = "Claude Features Digest"` for `claude-features-digest-YYYY-MM-DD.md`. That file is sitting in `Brain/raw/` today (`claude-features-digest-2026-08-02.md`, 3220 bytes). `wiki_ingest`'s cron was removed 2026-07-14; `brain_organizer` has no equivalent rule, so the digest routes through Haiku like any other note, and the "must land on its own running-log page, not be merged" reasoning in that docstring is currently unenforced.

Adding a Slack export or a voice-memo transcript pipeline therefore means reading the whole 1603-line file to discover the three sites, plus knowing that a fourth lives in a module that no longer runs.

### 7.2 Fix — one table, not a plugin system

Add a single module-level list near the top of `brain_organizer.py`:

```python
# Deterministic source routes, evaluated in order BEFORE the Haiku router.
# Add a row to onboard a new raw-note source; nothing else in this file changes.
_SOURCE_ROUTES: list[tuple[str, Callable[[str], bool], Callable[..., list[...]]]] = [
    ("daily_note",      _is_daily_note,        _daily_note_route),
    ("features_digest", _is_features_digest,   _features_digest_route),
]
```

and one dispatcher `_deterministic_route(stem, catalog, wiki_folder, daily_folder)` that walks it and returns the first hit, replacing the bare `_daily_note_route(...) or` at `:1216` and `:1447`.

`_is_daily_note`/`_daily_note_route` become row one, **unchanged in body**, so all 8 existing tests and the `wiki_ingest` mirror are untouched. Row two ports the features-digest rule from `wiki_ingest.py:53`–`:73` into the module that actually runs — that is a real, currently-unrouted file shape, not a hypothetical.

Deliberately *not* a plugin system, entry points, or a registry with hooks: a module-level list read top-to-bottom is the smallest thing that makes the seam findable, and it stays stdlib.

### 7.3 Acceptance criteria

51. `_deterministic_route` returns `_daily_note_route`'s exact result for every stem the 8 existing daily-note tests cover.
52. `_deterministic_route("claude-features-digest-2026-08-02", …)` resolves to `wiki/Claude-Features-Digest.md` with the digest's own date available for the section header (matching `wiki_ingest._digest_date`'s contract at `:64`).
53. `_deterministic_route` returns `None` for an ordinary topical stem, so `route_topics` still runs.
54. A test asserts every row in `_SOURCE_ROUTES` has a matching test — an added row without a test fails the suite. *(Same drift-guard pattern as `backend/safety/contracts.py::test_every_integration_is_covered`, which CLAUDE.md documents as already earning its keep.)*

---

## 8. F8 — Cross-file duplication, measured

The companion spec's §7.7 flagged `consolidate_wiki.py` as divergent-but-acceptable. It is worse than structural — **two of the copies have already drifted in behavior**, and there are six sites, not one.

| Logic | Copies | Drifted? |
|---|---|---|
| `_normalize_title` | `brain_organizer:239`, `consolidate_wiki:74` | **Yes.** `consolidate` adds `"ment"` to `_SUFFIXES` (`:81`). `"Deployment"` → `"deploy"` there, `"deployment"` here. The two tools cluster the vault differently. |
| `_extract_page_entry` | `brain_organizer:265`, `consolidate_wiki:96` | **Yes.** On a `#`-prefixed line during summary collection the organizer `break`s (`:300`–`:301`); `consolidate` `continue`s (`:141`–`:142`). Different summary text for the same file. `consolidate` also hardcodes 300 and adds a `chars` key. |
| `_is_daily_note` + 3 regexes | `brain_organizer:617`, `wiki_ingest:75` | In sync today — but F1 required editing both, and nothing enforces it. |
| Wikilink regex + normalization | `brain_organizer:860`/`:863`, `mcp_server:40`/`:118` | Opposite namespaces — see §3. |
| `_make_temp_path` + atomic JSON write | `brain_organizer:92`/`:113`, `consolidate_wiki:245`/`:249` | Equivalent. |
| `load_config` | `brain_organizer:61`, `consolidate_wiki:64`, `mcp_server:49` | Identical × 3 — and after §4 they must all gain validation. |

### 8.1 Fix

**Do not create a shared package.** `mcp_server.py:33` already imports from `brain_organizer` and `tests/conftest.py:13` already puts the module root on `sys.path` — the pattern exists and is cycle-free. Make `consolidate_wiki.py` do the same for `_normalize_title`, `_extract_page_entry`, `_make_temp_path`, and `load_config`; delete its four copies. Keep a thin local wrapper adding `chars` (a genuine consolidate-only need).

Cost is one line of risk: `consolidate_wiki` gains `httpx` as a transitive import. Both live in the same venv (`requirements.txt`), so this is free.

### 8.2 The `"ment"` question is a real decision, not a merge conflict

`consolidate_wiki`'s stemmer is strictly better for its purpose (a human reviews before `--apply`). Adopting `"ment"` into the shared version changes `find_similar_page`'s behavior on the nightly path. **Recommendation: adopt it**, and add it to the §5 similarity tests — `"Deployment"`/`"Deploy"` collapsing is the correct near-duplicate judgement, and the near-dup guard's whole job is catching synonym pages. But make it an explicit, tested change with a one-line comment, not an accidental side effect of deduplication.

### 8.3 The `wiki_ingest` mirror

`brain_organizer` runs in its own venv (`modules/brain-organizer/venv`); `wiki_ingest` runs in NEXUS's. A shared import across venvs is not available. So: a **drift test** in NEXUS's own suite (`tests/`) that reads both source files and asserts the three regex literals and the `_is_daily_note` body are textually identical. Crude, but it is the only mechanism that would have caught F1 at edit time, and it costs ~15 lines.

### 8.4 Acceptance criteria

55. `consolidate_wiki.py` defines no `_normalize_title`, `_make_temp_path`, or `load_config`; they are imported.
56. `consolidate_wiki._extract_page_entry` is a wrapper that calls the shared one and adds `chars`; its `summary` now matches the organizer's byte-for-byte for the same input.
57. `_normalize_title("Deployment") == _normalize_title("Deploy")`, and a comment at `_STEM_SUFFIXES` records that `"ment"` was adopted from `consolidate_wiki` deliberately.
58. A NEXUS-side test fails if `brain_organizer._is_daily_note`'s source text or any of the three daily-note regexes diverge from `wiki_ingest`'s.
59. `python consolidate_wiki.py` (dry run, no `--apply`) still produces a valid `consolidation-plan.json` against a tmp wiki.

---

## 9. Scope boundaries — explicitly OUT, and why

1. **The two companion-spec bugs.** §2.2, §4.1–§4.5 of `brain-organizer-wikilink-router-fixes-spec.md`. Not re-planned here. §3 and §5 of this spec assume that spec lands and are written to compose with it, not conflict.
2. **Retroactive content repair.** The 5 digest sections in `AdGuard.md`, the Unraid facts in `Channels-DVR.md`, the 888 legacy rename links. All manual content decisions for Brian, matching the companion spec's §4.6/§7.1. The pipeline never un-merges pages.
3. **A rewrite, a framework, or any new dependency.** Every recommendation is stdlib + the existing `anthropic`/`httpx`/`dotenv`/`flask`. No pydantic for §4 — a dict-walk validator is ~40 lines and adds nothing to the dependency surface. No `structlog`, no metrics library for §6 — the existing Telegram summary and `logging` carry it.
4. **A shared `brainlib/` package.** Considered and rejected in §8.1. Two import edges (`mcp_server` → `brain_organizer`, `consolidate_wiki` → `brain_organizer`) do the same job with zero packaging, zero venv changes, and zero new files.
5. **A required schema / frontmatter contract for inbound raw notes.** Tempting, and wrong. Three machine emitters exist already (`event-nexus-`, `event-hermes-`, `event-council-loop-`), plus Quill, plus `facts_digest`, plus Brian hand-creating files in Obsidian (`Mulch-Needs.md`, dropped in at 10:45 today). A required envelope breaks the human path, which is the one that must never break. §7's stem-pattern table achieves source-awareness without imposing a contract on the writer.
6. **Merging the inbound and outbound link scopes.** They differ for two separately documented, separately correct reasons (`mcp_server.py:85`–`:99`; `brain_organizer.py:646`–`:657`). §3.3(b) makes the difference explicit and tested rather than eliminating it.
7. **Re-arming `wiki_ingest`'s cron or its file observer.** CLAUDE.md documents an unresolved ownership conflict over `Brain/raw/`. §7.2 ports one *rule* out of it; it does not revive the module. The Sunday `wiki_fragmentation_report` (a second writer into `Brain/wiki/Inbox.md`, 30 minutes after the organizer's 02:00 run) is noted here as a known second writer but is out of scope.
8. **An Obsidian/Vault emitter for outcome flags.** `outcome-tracker-spec.md:249` Phase 2. Separate decision, separate spec.
9. **Any change to `_call_api`'s retry/OpenRouter path, `_record_usage`/`brain_spend.py`, `_group_files_by_shared_pages`, `_prune_old_backups`, the atomic-write machinery, or the `.organizer.lock` singleton.** All examined; all sound.
10. **`migrate_daily_pages.py`.** One-time migration, already run, has its own tests.

---

## 10. Suggested implementation order

| Step | Contents | Why here |
|---|---|---|
| 1 | §1 (F1 daily-note prefix) + §8.3 drift test | Live, actively corrupting pages nightly. Two-line diff. Ship tonight. |
| 2 | §2 (F2 empty-file + filename-aware ledger) | Live silent data loss. Small, self-contained, no interaction with the companion spec. |
| 3 | §6.5 (log rotation + the event-loop read) | One line each, unblocks nothing but stops a growing problem. |
| 4 | §5.3 catalog + similarity + 5b tests (28–44) | **Before** the companion spec touches `_extract_page_entry`, `build_wiki_catalog`, or the splicer. These are the regression net that spec is currently missing. Criteria 32 and 41–43 are shared. |
| 5 | — | **Companion spec ships here.** |
| 6 | §4 (config validation + defaults + `summary_chars`) | Closes the same hole the companion spec found in `new_page_similarity_threshold`, generally. |
| 7 | §6 (run-summary counters + empty-catalog abort) | Depends on §4's threaded config and on the companion spec's normalizer returning counts. |
| 8 | §3 (shared link resolver, broken-links footer) | Depends on the companion spec's stem-space rewrite existing. |
| 9 | §7 (`_SOURCE_ROUTES` table) | Pure refactor + one new row; safest last. |
| 10 | §8 (consolidate_wiki dedup, `"ment"` adoption) | Touches the nightly similarity behavior; do it with §5's similarity tests already green. |

Steps 1–3 are independently shippable tonight and do not conflict with the companion spec at any line.

---

### Critical Files for Implementation

- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\brain_organizer.py` — §1 (`:601`, `:632`), §2 (`:191`–`:204`), §4 (`_CONFIG_DEFAULTS`, `:326`/`:355`, 11 `config.get` sites), §6 (`:81`, `:1403`, `:1560`), §7 (`:604`/`:617`/`:636`/`:1216`/`:1447`)
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\tests\test_organizer.py` + `tests\conftest.py` — §5's 17 new tests; `tmp_config` rebuilt from `_CONFIG_DEFAULTS` (conftest `:54`–`:72`)
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\mcp_server.py` — §3 (`:40`, `:78`, `:84`, `:118`, `:147`), §4 (`create_app`, `:167`)
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\consolidate_wiki.py` — §8 (delete `:74`, `:96`, `:245`, `:249`, `:64`; import instead)
- `C:\Users\Brian\Documents\Agentic os\nexus\backend\agents\wiki_ingest.py` — §1.4 mirror (`:75`–`:95`), §7.2 source rule (`:53`–`:73`); plus `backend\api\brain_organizer.py:73` for §6.5
