# Brain Organizer — Wikilink Namespace & Router Visibility Fixes — Implementation Spec v1

**Role:** Opus planner. **Next role:** Sonnet writer (Council-loop or direct).
**Repo:** `C:\Users\Brian\Documents\Agentic os\nexus`
**Target file:** `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\brain_organizer.py` (1603 lines)
**Live vault:** `C:\Users\Brian\iCloudDrive\iCloud~md~obsidian\Brain\` — 269 wiki pages, git-ignored, iCloud-only. Pipeline runs unattended at 02:00 via `backend/scheduler.py::_run_brain_organizer`.

---

## 0. What I actually found (correcting the audit's framing)

The audit's two bugs are real, but the code shows **four** defects, and two of them are the actual mechanism behind the two the audit named. All numbers below are measured against the live vault and the live catalog cache (`Brain/_meta/wiki-catalog.json`, built 2026-08-02T06:00:05Z), not estimated.

| # | Defect | Location | Measured impact |
|---|---|---|---|
| **A** | The whole module speaks **titles**; Obsidian resolves **filenames**. The CREATE prompt literally seeds title-form links, and `_defuse_unknown_wikilinks` validates in title-space so it certifies them as good. | `:1082`, `:884` | **112** broken links, exactly the audit's bug 1 |
| **B** | Router prompt truncates an **alphabetically sorted** catalog at 60 of 269 pages. `NEXUS.md` is at position **158** — structurally unreachable. | `:360`, `:707` | **209 of 269 pages (78%) can never be merged into**; this is the audit's bug 2 |
| **C** | `_extract_page_entry` reads `encoding="utf-8"`, so a UTF-8 BOM defeats the `startswith("# ")` H1 check and the parser picks a stray mid-document H1 instead. | `:270`, `:277` | **58** BOM files, **19** mis-titled pages, incl. `NEXUS.md` → `"NEXUS Dashboard Card"`, `Git.md` → `"Force-push local branch onto a differently named remote branch"` |
| **D** | Branch 5b (large-page splice) returns at `:989`/`:1025`, **before** the `_defuse_unknown_wikilinks` call at `:1134`. Pages >20 000 chars never get any link processing at all. | `:951`–`:1025` | **43** pages always take this path; they hold **457 of 1014 (45%)** of all broken links |

C and D are not scope creep — C is why the router's menu shows `NEXUS.md` under a wrong, narrow title, and D is why any bug-1 fix would miss the largest and most heavily linked pages (`NEXUS.md` 75 KB, `Audit-Findings-and-Fixes.md` 117 KB, `Build-Log.md` 79 KB). Both are prerequisites, not extras.

### 0.1 Broken-link census — establishes the scope boundary numerically

Scanning all 269 pages, 2044 wikilinks total, 1014 that resolve to no filename:

| Category | Count | Cause | In scope? |
|---|---|---|---|
| Target is the **exact `title`** of a real page | **112** | Defect A — this pipeline | **Yes** |
| Target is already hyphenated but no such file (`[[Pricing-Calculator]]`, `[[AI-Automation-Business-Plan]]`) | **888** | The one-time `processed/` rename migration | **No — §7** |
| Target is a `wiki/daily/` date page (outside the catalog's non-recursive glob) | **14** | Known narrow gap | **No — §7** |

The 888 are already in filename form, which independently confirms they are legacy renames rather than the title-vs-filename bug. §7 makes this boundary explicit so Council-loop cannot drift into a content migration.

---

## 1. Root cause of bug 1 — stated from the code, not the audit

The audit guessed this was "a training-data-shaped LLM habit." It is not. **The pipeline explicitly instructs the model to write the broken form.**

**1.1 The prompt seeds title-form links.** `synthesize_wiki`'s CREATE branch (5c) builds `related_block` at `:1064`–`:1089`. Line `:1082` is:

```python
+ ", ".join(f"[[{t}]]" for t in top5)
```

where `top5` is drawn from `entry["title"]` at `:1076`. It is followed at `:1084` by "Use `[[wikilinks]]` **ONLY** for titles from this exact list." So when the related list contains `Bug-Fixes.md` (title `"Bug Fixes"`) or `Build-Log.md` (title `"Build Log: CWI AI — Passes 1–13, 13–32"`), the model is handed `[[Bug Fixes]]` and `[[Build Log: CWI AI — Passes 1–13, 13–32]]` and told to use exactly those. It complies. **143 of 269 catalog entries have `title != Path(filename).stem`**, so this misfires on 53% of the wiki. The em-dash class the audit counted separately is not a separate bug — it is the same bug, because `sanitize_topic_name` (`:225`) strips `—`/`&` via `[^\w\s\-]` when creating the file, while the H1 inside keeps them.

**1.2 `_defuse_unknown_wikilinks` cannot catch it, by construction.** At `:884`:

```python
known_titles = {p["title"] for p in catalog}
```

and at `:891`, `if target in known_titles: return m.group(0)`. `[[Bug Fixes]]` **is** a known title, so the function returns it untouched. The guard does not "miss" the case — **it affirmatively certifies the broken link as valid.** It is a *hallucination* filter (does a page by this name exist?) and the bug is a *namespace* error (title-space vs filename-space); the two questions never intersect.

**1.3 The `find_similar_page` fallback cannot catch it either.** `:893` calls `find_similar_page(target, catalog)`, which at `:401`/`:409` compares `_normalize_title(target)` against `_normalize_title(entry["title"])` — title-space on both sides, and `_normalize_title` strips all punctuation at `:248`, so em-dash and hyphen forms are *identical* after normalization. `[[Bug Fixes]]` self-matches at ratio 1.0. Both gates pass. There is no filename anywhere in either code path, even though every catalog entry already carries a `filename` field (`:315`).

**1.4 Verified: the rewrite is unambiguous.** Across the live catalog, **zero** page titles collide with a *different* page's filename stem, and only 2 duplicate titles exist (handled by the existing first-wins convention at `:770`). A title → stem rewrite is therefore deterministically safe.

---

## 2. Fix for bug 1

Both halves are required. The prompt change alone is an LLM instruction and will never be 100%; the deterministic pass is the guarantee. This follows the module's own stated convention — see `_defuse_unknown_wikilinks`'s docstring at `:874` ("Never rely on the prompt instruction alone… same deterministic-backstop pattern as the daily-note guard").

### 2.1 Make the wikilink regex preserve heading fragments

`_WIKILINK_PAT` at `:860` is:

```python
re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
```

The `#heading` fragment is in a **non-capturing** group, so any rewrite silently drops it. Make it capturing (3 groups: target, heading, alias) and re-emit it. Also add a negative lookbehind for `!` so an embed `![[...]]` is never rewritten.

### 2.2 Replace the title-space check with an ordered filename resolver

Extend `_defuse_unknown_wikilinks` in place (keep the name — it is referenced by 6 existing tests and by `synthesize_wiki`). Build both indexes once per call from the catalog:

```python
by_stem  = {Path(p["filename"]).stem: ...}       # canonical target space
by_title = {p["title"]: Path(p["filename"]).stem}  # first-wins, matches :770
```

Resolution order for each link target, first match wins:

| # | Test | Action |
|---|---|---|
| 1 | `target` is an exact filename stem | leave unchanged (already correct) |
| 2 | `target == topic` (self-reference) | leave unchanged — preserves `:885` and `test_defuse_unknown_wikilinks_allows_self_reference` |
| 3 | `target` is an exact catalog **title** | **rewrite to that page's stem** |
| 4 | case-insensitive stem match | rewrite to the canonical stem |
| 5 | case-insensitive title match | rewrite to that page's stem |
| 6 | `find_similar_page(target, catalog)` returns an entry | **rewrite to that entry's stem** (today it wrongly leaves the title-form link intact) |
| 7 | otherwise | backtick — unchanged existing behavior |

**Alias policy:** when rewriting, emit `[[<stem>#<heading>|<display>]]` where `<display>` is the original alias if present, else the original target text. Omit the alias entirely when it would equal the stem, so `[[NEXUS]]` never becomes `[[NEXUS|NEXUS]]`. Readability is preserved: `[[Bug Fixes]]` → `[[Bug-Fixes|Bug Fixes]]`.

Pass `config.get("new_page_similarity_threshold", 0.82)` into the step-6 `find_similar_page` call rather than relying on its default — the config value is currently ignored on this path.

### 2.3 Emit the correct form in the CREATE prompt

At `:1082`, change the related list to the exact final form the model should copy, and update the instruction text at `:1084` to say "use the exact link text shown, including the hyphenated target before the `|`."

```
Related pages in this wiki: [[Bug-Fixes|Bug Fixes]], [[Build-Log|Build Log: CWI AI — Passes 1–13, 13–32]], …
```

### 2.4 Apply the pass on **all** synthesis return paths (defect D)

`synthesize_wiki` currently normalizes only at `:1134`, which branch 5b never reaches. Add the call to:

- `:989` — the `NO_CHANGES` path. Return `existing_content` **unmodified**; do *not* normalize here. Normalizing would rewrite the whole page as a side effect of a no-op merge, turning a quiet night into a 43-page diff. Explicitly out of scope (§7).
- `:1025` — the spliced result. **Normalize before returning.** This is the path that carries 45% of the broken links.

Scope the 5b normalization to the newly spliced `## ` chunks only, not the whole document, for the same reason: the pipeline must fix links it *writes*, never retroactively rewrite untouched prose. Simplest correct implementation: normalize each `chunk` inside the loop at `:1000`–`:1023` before splicing.

---

## 3. Root cause of bug 2 — it is structural, not a Haiku misjudgment

The brief offered three hypotheses. The answer is hypothesis 2 (`NEXUS.md` is excluded from the candidate list) — but the mechanism is **alphabetical truncation**, not a size or token cap.

**3.1 The catalog is sorted alphabetically, then truncated.** `build_wiki_catalog` at `:360`:

```python
pages.sort(key=lambda p: p["title"].lower())
```

`route_topics` at `:706`–`:707`:

```python
max_in_prompt: int = config.get("catalog_max_pages_in_prompt", 60)
catalog_pages = catalog[:max_in_prompt]
```

`config.json` sets `catalog_max_pages_in_prompt: 60`. The vault has **269** pages. The router therefore sees only the alphabetically-first 60.

**3.2 Measured cutoff.** Slot 60 is `CRM`. Slot 61 is `Cushing's Disease (Hyperadrenocorticism) — Canine`. The first-letter distribution of the 60 visible slots is `{9: 1, A: 15, B: 14, C: 30}`. **The router's entire universe of existing pages is titles beginning with A, B, or C.** 209 of 269 pages (78%) are permanently invisible.

**3.3 The three misrouting destinations are exactly the visible ones.**

| Page | Alphabetical rank | In prompt? |
|---|---|---|
| `Agentic-OS.md` | **3** | Yes |
| `Audit-Findings-and-Fixes.md` | **15** | Yes |
| `Build-Log.md` | **26** | Yes |
| **`NEXUS.md`** | **158** | **Never** |

**3.4 Independently confirmed in the live ledger.** Every existing-page route recorded in `processed.json` since 2026-07-31 lands on an A/B/C title — `Agentic OS`, `AdGuard`, `Audit Findings and Fixes`, `Bug Fixes`, `Build Log`, `Brain`, `Brain Organizer`, `Channels DVR`, `Claude Code`, `Claude Features Digest`, `Compliance`, `Cost Optimization`, `Council Loop`, `Business Operations`, `Applications Reference`, `BeyondTrust`, `Carnivore Tracker`. The only non-A/B/C entries are `match:"new"` titles, which are not menu-constrained. **This is not sampling noise — it is the shape of the bug.** Haiku's decision was correct given its menu; the correct answer was never on the menu.

**3.5 The near-dup escape hatch also fails for NEXUS.** When Haiku returns a title not in `by_title`, `:799` re-routes it through `find_similar_page` against the **full** catalog (`:807`) — a genuine partial mitigation. But for NEXUS it cannot fire: `_normalize_title`'s stemmer strips the trailing `s`, so `"NEXUS"` → `"nexu"`, a single token. Against `"NEXUS Dashboard Card"` → `"nexu dashboard card"` the SequenceMatcher ratio is ≈0.35, far under the 0.82 threshold, and the Jaccard boost at `:417` requires **both** titles to be multi-word, which `"nexu"` is not. No match.

**3.6 Defect C makes 3.5 worse and is a separate bug.** `NEXUS.md`'s real first line is `\ufeff# NEXUS` — a UTF-8 BOM. `_extract_page_entry` reads with `encoding="utf-8"` at `:270`, so the BOM survives into the string, `"\ufeff# NEXUS".startswith("# ")` is `False`, the loop at `:276`–`:281` skips the real H1, and continues to line 802's stray `# NEXUS Dashboard Card`. **The page's registered title across the catalog, the router menu, `by_title`, `find_similar_page`, and `known_titles` is `"NEXUS Dashboard Card"`.** 58 files carry a BOM; 19 are mis-titled. The severe ones:

```
AI.md          -> 'open a new PowerShell window'
Backup.md      -> 'VM snapshot (NEXUS, VM 101)'
Git.md         -> 'Force-push local branch onto a differently named remote branch'
PowerShell.md  -> 'Run a script'
NEXUS.md       -> 'NEXUS Dashboard Card'
```

This also feeds bug 1: a correct `[[AI]]` link is defused to `` `AI` `` because `AI.md`'s registered title is a PowerShell sentence.

**3.7 What is *not* the cause.** Not the 77 KB size — nothing in `route_topics` reads page size. Not a token cap — the cap is a page *count*. And `Agentic-OS.md` is **not** a near-duplicate of `NEXUS.md` that "wins": it is a genuinely distinct, legitimately overlapping page (`# Agentic OS`, 94 headings, control-loop/safety/goals framing) versus `NEXUS.md` (155 headings, dashboard/widgets/integrations/build-log framing). Deciding their boundary is a content judgement, not a code fix — see §4.5.

---

## 4. Fix for bug 2

### 4.1 Fix the BOM (defect C) — `_extract_page_entry`, `:270`

Change `encoding="utf-8"` to `encoding="utf-8-sig"`. This is the correct, minimal fix: `utf-8-sig` decodes BOM-less files identically, so it is a no-op for the other 211 pages. Do **not** hand-strip `\ufeff`.

### 4.2 Invalidate the catalog cache when the parser changes — `build_wiki_catalog`

**This is the easiest step in the whole spec to get wrong.** `:352` reuses a cached entry whenever `f.stat().st_mtime <= built_at_ts`. After 4.1, all 269 cached entries are stale, but **no mtime changes**, so the BOM fix would have zero effect on the live vault — the wrong titles would persist indefinitely.

Add a parser-version constant (e.g. `_CATALOG_PARSER_VERSION = 2`), write it into `wiki-catalog.json` alongside `built_at` at `:368`, and treat a missing or mismatched version as a full cache miss (equivalent to today's `built_at_ts = 0.0` path at `:346`). Bump it whenever `_extract_page_entry`'s output shape or parsing changes.

### 4.3 Select the rich prompt window by **relevance**, not alphabet — `route_topics`, `:706`–`:718`

Replace `catalog[:max_in_prompt]` with a deterministic, LLM-free lexical ranking of the **full** catalog against the note content — the same shape as the ranking already in the CREATE branch at `:1069`–`:1078`, so this extends an existing pattern rather than inventing one.

Suggested scorer (stdlib only, no new dependency, matching the module's stdlib-only convention): tokenize `content[:3000]` and each entry's `title + headers + summary` to lowercase alphanumeric words of length >2 with a small stopword set; score by summed IDF over the intersection, IDF computed across the catalog. Sort descending, take `max_in_prompt`.

**Validated against the live vault**, scoring the real `docs/outcome-tracker-spec.md` (a genuine NEXUS-topic note) against the real 269-page catalog:

```
NEXUS.md        alphabetical rank 158  ->  relevance rank 22   (now inside the 60 window)
Agentic-OS.md   alphabetical rank   3  ->  relevance rank 19
```

Both land on the menu, which is the point. The writer should **not** tune the scorer for precision — §4.4 provides the correctness guarantee, and this step only needs recall.

### 4.4 Append a full title-only index — the actual guarantee

Ranking quality is a heuristic; visibility must not be. After the rich block, append every remaining page as `Title (filename-stem)` on one line each, under a header such as `ALL OTHER PAGES (title — file):`, with the same "prefer existing" instruction.

**Measured cost:** all 269 titles = 6 928 chars ≈ **1 732 tokens**. Against ~30 routing calls/night on Haiku 4.5 at $1/Mtok input, that is **≈ $0.05/night (~$1.60/month)** — see the `brain_spend.py` handoff already wired in `_record_usage` (`:437`). For contrast, promoting all 269 to *rich* entries would cost 71 351 chars ≈ 17 837 tokens (~10×) for no additional recall. Take the cheap option.

After this change **every page in the vault is nameable by the router**, independent of scorer quality — which is the property that actually fixes bug 2.

### 4.5 Make the existing-title lookup tolerant — `route_topics`, `:769`–`:793`

`by_title` is exact-match only, so a router answer naming a page's *filename stem* (now visible via 4.4) falls through to the hallucination path at `:796`. Extend the lookup, first match wins: exact title → exact filename stem → case-insensitive title → case-insensitive stem. Only then fall through to the existing `match = "new"` re-check at `:799`, which is unchanged.

### 4.6 Separate manual step — NOT the pipeline

`NEXUS.md` (75 KB, 155 headings, last written 2026-07-15) and `Agentic-OS.md` (46 KB, 94 headings, mtime 2026-08-02) genuinely overlap. **Do not have the pipeline auto-merge two pages.** Flag for Brian as a one-time manual content decision, out of this spec's code scope:

1. Decide whether `Agentic-OS.md` is a distinct topic or should fold into `NEXUS.md`.
2. If they stay separate, sharpen each page's H1 and lead paragraph so the §4.3 scorer and Haiku can tell them apart.
3. Confirm `NEXUS.md`'s H1 should be `# NEXUS` (the BOM fix in §4.1 restores this automatically) and consider deleting or demoting the stray mid-document `# NEXUS Dashboard Card` at line 802, plus the three other stray H1s at lines 905/982/1038 — a markdown page should have exactly one H1.

Once §4.1–§4.5 ship, NEXUS-topic content will be *able* to route to `NEXUS.md`. Whether it *should*, versus `Agentic-OS.md`, is the content question above.

---

## 5. Regression safety — how this is proven before it runs live

The vault is git-ignored, iCloud-synced, and **wiki pages are never backed up** — only raw files are (`backup_file`, `:212`). The code itself notes at `:1121` that a bad write is "unrecoverable except via iCloud versioning." Treat every step below as mandatory and ordered.

**5.1 Unit tests first, in the existing harness.** `modules/brain-organizer/tests/` uses pytest with `tmp_vault` / `tmp_config` fixtures (`conftest.py`) and a `MagicMock` Anthropic client whose `messages.create.side_effect` returns canned `TextBlock`s via `_make_message`. New tests go in `tests/test_organizer.py` under new banner comments matching the existing `# ---` section style. **Never point a test at the real vault path.** The autouse `_no_real_secrets_in_tests` fixture must keep passing untouched.

**5.2 Read-only dry run against a real catalog copy.** Copy `Brain/_meta/wiki-catalog.json` into a scratch dir and, with **no API calls and no writes**, assert:
- the new resolver rewrites exactly the **112** category-A links and leaves the **888** category-D and **14** category-C links as backticks or unchanged (§0.1 baseline);
- `_extract_page_entry` with `utf-8-sig` produces the corrected title for all **19** mis-titled files;
- the §4.3 ranking places `NEXUS.md` inside `catalog_max_pages_in_prompt` for a NEXUS-topic note.

**5.3 Snapshot-vault end-to-end run — the gate before live.** Copy the entire `Brain/` tree to a scratch location (`%TEMP%\claude\...\scratchpad\vault-snapshot\`), point a copy of `config.json` at it, seed `raw/` with 3–5 representative notes (one NEXUS-topic, one that links to a title-form page, one >20 KB target to exercise branch 5b, one daily note to prove `_daily_note_route` is untouched), and run `run(_config=...)` with a **real** Anthropic client. Then diff snapshot-vault `wiki/` against the live `wiki/` and confirm:
- no page shrank unexpectedly (the `:1128` 50 % guard held);
- no *new* broken links were introduced;
- pages not targeted by any raw note are **byte-identical** — this is the single most important assertion, because it proves the change cannot silently rewrite 269 pages.

**5.4 One supervised live run.** Run manually and watch the log before re-arming the 02:00 scheduler job. `git diff` cannot help here, so capture a `robocopy /MIR` copy of `Brain/wiki/` immediately beforehand as the rollback.

**5.5 Rollback lever.** Gate §4.3/§4.4 behind a config key (e.g. `router_catalog_ranking: true` in `config.json`) so reverting to alphabetical truncation is a one-line config edit, not a code revert — matching the `outcome_flags_enabled` pattern used elsewhere in this repo.

---

## 6. Acceptance criteria

Written in the module's existing test style (`pytest`, `tmp_config`, `MagicMock`, `bo.` module alias).

### 6.1 Wikilink normalization

1. `_defuse_unknown_wikilinks("See [[Bug Fixes]].", "Other", [{"title": "Bug Fixes", "filename": "Bug-Fixes.md", ...}])` returns `"See [[Bug-Fixes|Bug Fixes]]."`
2. Em-dash case: target `"Build Log: CWI AI — Passes 1–13, 13–32"` with `filename="Build-Log.md"` → `[[Build-Log|Build Log: CWI AI — Passes 1–13, 13–32]]`.
3. A target that already equals a filename stem is returned **byte-identical** (regression guard for `test_defuse_unknown_wikilinks_leaves_real_catalog_page_alone`).
4. An unknown target still becomes backticked text, and an alias is still preserved as the display text (existing tests `..._converts_unknown_target_to_backticks` and `..._preserves_alias_display_text` pass unchanged).
5. Self-reference `[[<topic>]]` is unchanged (existing test passes unchanged).
6. **Intentional test change:** `test_defuse_unknown_wikilinks_allows_near_duplicate_via_find_similar_page` currently asserts `[[Financial Forecasting]]` stays untouched when `Financial-Forecast.md` exists. Under §2.2 step 6 it must now become `[[Financial-Forecast|Financial Forecasting]]`. Update the assertion and its comment; do not weaken the fix to preserve the old expectation.
7. A heading fragment survives: `[[Bug Fixes#Setup]]` → `[[Bug-Fixes#Setup|Bug Fixes]]`.
8. An embed `![[image.png]]` is never rewritten or backticked.
9. `synthesize_wiki`'s CREATE-branch `related_block` contains `[[Bug-Fixes|` and does **not** contain the bare string `[[Bug Fixes]]`.
10. **Branch 5b coverage:** a large-page merge (`existing_content` > `large_page_threshold_chars`) whose spliced `## ` chunk contains `[[Bug Fixes]]` produces `[[Bug-Fixes|Bug Fixes]]` in the returned document. A `NO_CHANGES` response still returns `existing_content` **byte-identical**.
11. Existing sections of a large page that were *not* spliced are byte-identical after the merge.

### 6.2 BOM / title extraction

12. `_extract_page_entry` on a file whose bytes begin `b"\xef\xbb\xbf# NEXUS\n\n## Overview\n"` returns `title == "NEXUS"` (today it returns the stem or a later stray H1).
13. A file with no BOM parses identically to before (no behavior change).
14. A file whose first `#` line is a tag line (`#audit #bugs …`, as in the real `Audit-Findings-and-Fixes.md`) still skips it and picks the following `# ` H1 — regression guard for the existing `startswith("# ")` semantics.
15. `build_wiki_catalog` re-parses every page when the cached JSON has a missing or mismatched parser version, even though no file mtime changed; and reuses the cache when the version matches and mtimes are older.

### 6.3 Router visibility

16. Given a synthetic 200-page catalog where the only relevant page is at alphabetical position 150, `route_topics`'s prompt (captured from the `MagicMock`'s `messages.create` call args) **contains** that page's title.
17. The prompt contains **every** catalog title in the title-only index, for a catalog larger than `catalog_max_pages_in_prompt`.
18. The rich block is still capped at `catalog_max_pages_in_prompt` entries.
19. A router response of `{"match": "existing", "title": "Bug-Fixes"}` (filename-stem form) resolves to the real page via §4.5 and is **not** logged as a hallucinated title.
20. With the §5.5 config flag off, the prompt is byte-identical to today's alphabetical-truncation output.

### 6.4 Non-regression on unrelated guards

21. All existing tests in `tests/test_organizer.py` pass, except the single intentional change in criterion 6.
22. `_looks_like_session_title` and its 2 parametrized tests are untouched.
23. `_is_daily_note` / `_daily_note_route` and their 8 tests are untouched.
24. `sanitize_topic_name` is unchanged — it is imported by `mcp_server.py:33` and used at `:291`.
25. `tests/test_mcp_server.py` and `tests/test_migrate_daily_pages.py` pass unchanged.

### 6.5 Live-vault outcome (measured after the supervised run in §5.4)

26. A re-run of the §0.1 census shows category A (exact-title broken links) **strictly decreasing** on every page the run touched, and **not increasing** on any page.
27. Category D stays at **888** — the pipeline must not touch the out-of-scope migration links.
28. The rebuilt `wiki-catalog.json` shows `NEXUS.md` with `title == "NEXUS"` and all 19 mis-titled pages corrected.

---

## 7. Scope boundaries — explicitly OUT

1. **The ~888 pre-existing broken links from the `processed/` rename.** These are already in filename form and are a one-time content migration, not a pipeline defect. The pipeline must not attempt to auto-repair them. Do not add a "fix all existing links" pass, a vault-wide rewriter, or a migration script to this change.
2. **The 14 `wiki/daily/` links.** `build_wiki_catalog`'s non-recursive `glob("*.md")` (`:350`) excludes the daily subfolder deliberately — the reasoning is documented at `:651`–`:657`. Do not widen the glob; that would put one page per calendar day back into the router's context, which is the exact problem `_daily_note_route` was built to solve.
3. **Auto-merging `Agentic-OS.md` into `NEXUS.md`.** Content work, manual, Brian's call (§4.6). The pipeline never merges two existing pages.
4. **Rewriting untouched pages.** Normalization applies only to synthesis output for pages the run is already writing. The `NO_CHANGES` path at `:989` must stay a true no-op.
5. **`_looks_like_session_title`, `_is_daily_note`, `_daily_note_route`.** Different bug classes, working correctly, do not touch.
6. **`_call_api` retry / OpenRouter fallback, `_record_usage`, `send_telegram_notification`, `_group_files_by_shared_pages`, `_prune_old_backups`, the atomic-write and `processed.json` ledger machinery.** Unrelated.
7. **`consolidate_wiki.py`.** It carries its own *copies* of `_normalize_title` and `_extract_page_entry` (`:74`, `:96`) rather than importing them. It is not on the nightly path. Leaving it divergent is acceptable for this change; note the divergence in a comment but do not refactor both into a shared module in this pass.
8. **No new third-party dependencies.** The module is stdlib + `anthropic` + `httpx` + `dotenv` only.
9. **No changes to `mcp_server.py`, `migrate_daily_pages.py`, or anything under `backend/`.**

---

## 8. Suggested implementation order

1. §4.1 BOM fix + §4.2 cache versioning (unblocks everything; smallest diff).
2. §2.1 regex + §2.2 resolver + tests 6.1/1–8.
3. §2.4 branch-5b coverage + tests 6.1/10–11.
4. §2.3 CREATE prompt + test 6.1/9.
5. §4.3 + §4.4 + §4.5 router visibility, behind the §5.5 flag + tests 6.3.
6. Full suite green (6.4), then §5.2 → §5.3 → §5.4.
7. Hand §4.6 to Brian as a separate manual content decision.

---

### Critical Files for Implementation

- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\brain_organizer.py` — all code changes; key sites `:270`, `:352`, `:360`, `:706`, `:769`, `:860`, `:884`, `:989`, `:1025`, `:1082`, `:1134`
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\tests\test_organizer.py` — all new tests; one intentional assertion change at `:398`
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\tests\conftest.py` — `tmp_vault` / `tmp_config` / `_no_real_secrets_in_tests` fixtures the new tests must reuse
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\config.json` — `catalog_max_pages_in_prompt`, `large_page_threshold_chars`, plus the new §5.5 rollback flag
- `C:\Users\Brian\iCloudDrive\iCloud~md~obsidian\Brain\_meta\wiki-catalog.json` — read-only ground truth for the §5.2 dry run and the §6.5 measurements
