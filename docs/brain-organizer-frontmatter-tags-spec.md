# Brain Organizer — Frontmatter Tags — Implementation Spec v1

**Role:** Opus planner. **Next role:** writer (Council-loop or direct).
**Repo:** `C:\Users\Brian\Documents\Agentic os\nexus`
**Target file:** `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\brain_organizer.py` (1603 lines)
**Live vault:** `C:\Users\Brian\iCloudDrive\iCloud~md~obsidian\Brain\wiki\` — 269 root pages.
**Companion specs:** `docs/brain-organizer-wikilink-router-fixes-spec.md`, `docs/brain-organizer-robustness-spec.md`. Neither touches tags or frontmatter. §7 below states how this composes with both.

---

## 0. Current state — measured, not assumed

### 0.1 The code is a blank slate

`brain_organizer.py` contains **zero** frontmatter handling. `_extract_page_entry` (`:265`) is hand-rolled line scanning — no YAML library, no `---` awareness at all. It returns exactly `{title, filename, path_str, headers, summary}`; the live `_meta/wiki-catalog.json` confirms those five keys and **no** tag-like field. `_meta/` holds no topics/tag vocabulary registry beyond `topics-registry.json` (topic → path only). The synthesis prompts never mention tags; nothing reads or writes them.

### 0.2 The vault is *not* a blank slate — but it is inconsistent

| Measurement | Count |
|---|---|
| Root wiki pages (`wiki/*.md`) | **269** |
| Pages with a top-of-file YAML frontmatter block | **44** |
| Pages with a `tags:` key in that block | **25** |
| Pages with **no** frontmatter at all | **225** |
| Distinct frontmatter tag values | **109** (67 appear exactly once) |
| Pages with a body-level inline `#tag #tag` line (separate corpus) | **33** (181 distinct tokens) |

Three incompatible `tags:` forms are live:

| Form | Pages | Example |
|---|---|---|
| YAML block list | **19** | `tags:`⏎`  - ford`⏎`  - beyondtrust` |
| Hash-string (**invalid YAML** — `#` opens a comment) | **4** | `tags: #compliance #legal #hipaa` (`HIPAA.md`, `Home-Automation.md`, `Insurance.md`, `Lead-Generation.md`) |
| Inline array | **2** | `tags: [risk-management, audit, ...]` (`Risk-Management.md`, `SOP-Builder-App-Specification.md`) |

**The block list is the measured majority (19 of 25), not the inline form.** Spec follows the data (§3.3).

### 0.3 Four document hazards a writer will hit

1. **UTF-8 BOM — 58 pages.** The companion spec's §4.1 fixes the reader; the writer here must still handle and preserve `\ufeff`.
2. **CRLF — 162 pages.** A writer emitting `\n` produces mixed line endings and a spurious full-file diff on an iCloud-synced vault.
3. **Leading ` ```markdown ` code fence — 3 pages** (`Audit-Findings-and-Fixes.md`, `BeyondTrust.md`, `MOC-AI-Business.md`). All three have a `---` block inside the fence, invisible to Obsidian as frontmatter.
4. **Mid-file embedded frontmatter — at least 3 pages** (`NEXUS.md` lines 898/975, `Obsidian.md` lines 658/888, `Sales.md` line 841). Any parser that regex-searches `^tags:` document-wide will hit these. Parsing must be anchored to position 0.

### 0.4 The finding that decides the design

Hazard 3 is load-bearing. Frontmatter currently survives only because the merge branch (5a) hands the whole existing document to Sonnet and asks for "the complete updated Wiki document" — the two most recently organizer-written frontmatter pages both came back wrapped in a fence. Branch 5b (splice, 43 pages) structurally cannot emit frontmatter at all.

**Therefore the LLM must never own frontmatter.** It proposes a tag list as JSON; code computes and writes the frontmatter — same division already used by `_looks_like_session_title`, `_defuse_unknown_wikilinks`, `_daily_note_route`.

### 0.5 One pre-existing catalog bug this must fix first

`_extract_page_entry`'s summary scan starts at `h1_index + 1`. For a page with no H1, `h1_index` is `0`, so the scan starts inside the frontmatter. Live proof: `Charlee Health Report.md` → `summary= "category: Reference date: 2026-06-10"`. **30 of 269 pages have no H1.** This spec would grow that leak toward 30 pages of frontmatter-as-summary feeding the router's prompt. Fixing the summary extractor is a prerequisite (§2.2).

---

## 1. Design

### 1.1 Vocabulary: derived from the catalog, no new registry file

Add a `tags` field to the catalog entry rather than a separate registry file — `_extract_page_entry` already reads every page's full text, so parsing frontmatter tags costs zero extra I/O, and the mtime cache refreshes tags exactly when content changes. `process_file`'s in-run catalog refresh means a tag minted on note 1 of a run is visible to note 4 of the same run.

Seed vocabulary = the 109 existing frontmatter tags. It grows only through §1.3's guard. Do **not** seed from the 33 body-level `#hashtag` lines (§6.6/§8.4).

### 1.2 Generation: one Haiku call per note, not per page

New `suggest_tags(content, existing_tags, vocabulary, config, client) -> list[str]`, called once per raw note in `process_file`, **not** folded into `route_topics` — widening that function's return tuple touches `_group_files_by_shared_pages`, `_daily_note_route`, `run()`, and four tests for no benefit.

**Measured cost:** ~1 400 input / ~50 output tokens per note. At ~30 notes/night on Haiku 4.5, ≈ $0.04/night (~$1.30/month) — already metered by `_record_usage`.

### 1.3 Anti-sprawl: a deterministic reconciler, not prompt discipline

`_reconcile_tags(proposed, vocabulary, existing, config) -> list[str]`. Never trust the prompt alone.

```python
def _normalize_tag(t: str) -> str:
    return _normalize_title(t.replace("-", " ").replace("_", " ").replace("/", " "))
```

Pipeline, in order:
1. Slugify: lowercase, strip, collapse whitespace/underscores to `-`, drop everything outside `[a-z0-9-]`.
2. Shape filter: length 2–30, not purely numeric, not a `category/*` value.
3. **Canonicalize:** if `_normalize_tag(proposed)` matches an existing vocabulary entry, replace with the vocabulary's spelling. The corpus spelling always wins.
4. Drop anything already in `existing`.
5. Allow at most `tag_max_new_per_note` (default **1**) tags not in the vocabulary. Surplus new tags dropped, not renamed.
6. Truncate to `tag_max_per_note` (default **5**).
7. If `len(existing) >= tag_max_per_page` (default **12**), return `[]` — growth stops, nothing is ever removed.

### 1.4 Merge semantics: additive, code-owned, never regressing

Union = tags parsed from the on-disk existing file ∪ reconciled new tags. Tags appearing in synthesized content are **ignored** — discarded and replaced wholesale, since the LLM doesn't own this field. Existing tags written back **verbatim** (never re-slugified), original order, new tags appended after. Stable ordering is what makes the no-op guarantee (criterion 14) achievable, preventing nightly frontmatter churn on an iCloud-synced vault.

---

## 2. Code changes — catalog layer

### 2.1 `_extract_page_entry` (`:265`) — parse frontmatter, expose `tags`

```python
_FM_MAX_LINES = 100

def _find_frontmatter(text: str) -> tuple[int, int] | None:
    """Return (first_body_line_index, closing_delimiter_line_index) or None."""
```

Contract: detect on `text.lstrip("\ufeff")`; return `None` if the first non-BOM, non-blank content is a ` ``` ` fence; require line 0 to be exactly `---`, and a later line exactly `---` within `_FM_MAX_LINES`, else `None`.

```python
def _parse_frontmatter_tags(text: str) -> list[str]:
```

Parses only inside `_find_frontmatter`'s block — **never** a document-wide `^tags:` search. Handles all three live forms (bracket array, hash-string, block list). Returns original spellings, de-duplicated case-insensitively, order preserved.

`_extract_page_entry` returns a sixth key: `"tags": _parse_frontmatter_tags(text)`.

### 2.2 Stop leaking frontmatter into `summary` (§0.5)

Compute `fm = _find_frontmatter(text)` once. Start both the title scan and the prose scan at `fm[1] + 1` when `fm` is not `None`. Prose scan start becomes `max(h1_index + 1, fm_end + 1)`.

### 2.3 `build_wiki_catalog` — bump the parser version

`_extract_page_entry`'s output shape changes with no mtime change. Bump `_CATALOG_PARSER_VERSION` (introduced by the robustness spec's §4.2; if this spec ships first, introduce the constant here and the robustness spec inherits it).

### 2.4 New — `_build_tag_vocabulary(catalog, limit) -> list[str]`

`collections.Counter` over every entry's `tags`; drop `category/*`; sort by `(-count, tag)`; return the first `limit` (`tag_vocabulary_max_in_prompt`, default **200**).

---

## 3. Code changes — write layer

### 3.1 New — `_render_tags_block(tags, newline) -> str`

Always emits the **block-list** form (majority live form, and what Obsidian's own tag UI writes) — including for pages currently using inline-array or hash-string form. One emission path.

### 3.2 New — `_write_frontmatter_tags(content, tags, *, original_frontmatter=None) -> str`

The only function that mutates frontmatter:

- **Newline fidelity:** `"\r\n"` if the document's first 2 000 chars contain it, else `"\n"`.
- **BOM fidelity:** operate on the remainder if `content` starts with `\ufeff`, re-prepend it.
- **Fenced document:** return `content` unchanged, log one WARNING. The 3 known-corrupt pages stay out of scope.
- **Block exists:** replace only the `tags:` region; every other key (`category:`, `date:`, `related:`, etc.) byte-identical.
- **Block exists, no `tags:` key:** insert immediately before the closing `---`.
- **No block, no `original_frontmatter`:** prepend a fresh block at position 0 (after any BOM).
- **No block, `original_frontmatter` supplied:** re-prepend the original block with its `tags:` region replaced (§3.4).
- **No-op:** tag list equals parsed existing list and block already canonical → return unchanged, byte for byte.

### 3.3 `process_file` — Phase 0 and the Phase 2 write

**Phase 0** (after the routing log, before Phase 1):

```python
note_tags: list[str] = []
if config.get("tags_enabled", True) and not all(
    p.is_relative_to(daily_folder) for (_t, p, _n) in routes
):
    try:
        vocab = _build_tag_vocabulary(catalog, config.get("tag_vocabulary_max_in_prompt", 200))
        note_tags = _reconcile_tags(
            suggest_tags(content, [], vocab, config, client), vocab, [], config
        )
    except Exception as exc:
        logger.warning("tag suggestion failed for %s: %s — continuing untagged", display_name, exc)
```

Best-effort — a tag-suggestion failure must never fail a note that would otherwise process cleanly.

**Skip rule:** if every route resolves under `daily_folder`, skip the call — date pages are a per-day journal, tagging 365/year is noise. `Daily-Log.md` (wiki root, non-date stem) is still tagged.

**Phase 1:** widen `topic_results` to 4-tuples carrying `existing_tags = _parse_frontmatter_tags(existing)`.

**Phase 2** (before the temp write):

```python
final_tags = existing_tags + [t for t in note_tags if t.lower() not in {e.lower() for e in existing_tags}]
final_tags = final_tags[: config.get("tag_max_per_page", 12)] if not existing_tags else final_tags
wiki_content = _write_frontmatter_tags(wiki_content, final_tags, original_frontmatter=orig_fm)
```

### 3.4 Guard: the merge branch dropping non-tag frontmatter keys

Branch 5a hands the whole document to Sonnet; if it drops the frontmatter block, `category`/`date`/`related`/`aliases` are lost silently across the 44 frontmatter pages. Capture `orig_fm` in Phase 1; in Phase 2, if `existing` had a block and the synthesized `wiki_content` has none, re-prepend `orig_fm` with its `tags:` region replaced rather than writing a tags-only block. ~5 lines, prevents a real regression class.

---

## 4. Prompt change — `suggest_tags`

Modeled on `route_topics`: system text folded into the user message, JSON-only response, markdown-fence stripping, fallback to `[]` on any parse failure.

```
You are a tagging assistant for a personal wiki. Assign topic tags to a note.

EXISTING TAG VOCABULARY (prefer these; they are the tags already used in this wiki):
<vocabulary, comma-separated>

NOTE:
<content[:3000]>

Return ONLY a JSON object, no other text:
{"tags": ["tag-one", "tag-two"]}

Rules:
- Return 1 to 5 tags.
- STRONGLY prefer tags from the vocabulary above, spelled EXACTLY as shown.
- Propose a NEW tag only when NO vocabulary tag genuinely fits the note's subject.
- Never propose a near-synonym of a vocabulary tag (e.g. do not invent
  "home-assistant-setup" when "home-automation" exists).
- Tags are lowercase, hyphen-separated, single concepts. No "category/..." tags.
- Tag the note's SUBJECT, not its format (no "note", "session", "log", "digest").
```

Uses `config["haiku_model"]` and `config.get("route_max_tokens", 1024)`.

---

## 5. Config

Five new keys in `config.json` and `tests/conftest.py`'s `tmp_config`:

| Key | Default | Purpose |
|---|---|---|
| `tags_enabled` | `true` | Single rollback lever. `false` ⇒ output byte-identical to today. |
| `tag_max_per_note` | `5` | Cap on tags contributed by one note. |
| `tag_max_new_per_note` | `1` | Cap on tags not already in the vocabulary — the anti-sprawl teeth. |
| `tag_max_per_page` | `12` | Soft growth ceiling; never removes. |
| `tag_vocabulary_max_in_prompt` | `200` | Prompt budget. |

If the robustness spec's `_CONFIG_DEFAULTS` has already shipped, add these there and use `config[k]`.

---

## 6. Acceptance criteria

Written in the module's existing test style (`pytest`, `tmp_config`/`tmp_vault`, `MagicMock`, `bo.` module alias, `make_message` helper).

### 6.1 Frontmatter parsing
1. Block-list form parses correctly.
2. Inline-array form (quoted and unquoted) parses correctly.
3. Hash-string form parses correctly.
4. A document whose only `tags:` line is mid-file (e.g. real `NEXUS.md` shape) returns `[]` — position-0 anchoring.
5. A fence-led document with `---`/`tags:` inside returns `[]` and `_find_frontmatter` returns `None`.
6. A `---` opener with no closing `---` within `_FM_MAX_LINES` returns `[]`.
7. BOM-prefixed frontmatter parses identically to the same bytes without the BOM.

### 6.2 Catalog integration
8. `_extract_page_entry` returns a `tags` key; `[]` when no frontmatter.
9. **Regression fix:** a page with frontmatter and no H1 returns a real summary, not the frontmatter text (the live `Charlee Health Report.md` bug).
10. A page with an H1 after frontmatter parses `title`/`headers`/`summary` byte-identically to before.
11. `build_wiki_catalog` treats a cache written under the previous parser version as a full miss.
12. `_build_tag_vocabulary` orders by descending frequency then alphabetically, excludes `category/*`, honors `limit`.

### 6.3 Frontmatter writing
13. No existing frontmatter → block prepended, rest byte-identical.
14. **No-op guarantee:** equal tag list + canonical existing block → byte-identical output.
15. Other frontmatter keys (`category:`, `date:`, `related:`) byte-identical after a tags-only change.
16. Frontmatter with no `tags:` key → block inserted before closing `---`, other keys untouched.
17. CRLF stays CRLF; LF stays LF — no mixed endings.
18. BOM stays the first byte.
19. Fence-led document returned unchanged + one WARNING logged.
20. Hash-string form rewritten to block list, values preserved.

### 6.4 Reconciliation
21. Proposed `"Home Automation"` + vocabulary `["home-automation"]` → `["home-automation"]`.
22. Proposed `"startups"` + vocabulary `["startup"]` → `["startup"]` (stemming).
23. Three brand-new proposed tags against empty vocabulary → exactly one survives.
24. `"category/projects"` proposed → dropped.
25. Tags already in `existing` dropped from the new-tags list.
26. `existing` at length 12 → result `[]`, nothing removed.
27. Six valid vocabulary tags → truncated to five.
28. Junk inputs (`""`, `"a"`, `"12345"`, `"###"`, 40-char string) dropped without raising.

### 6.5 Generation and pipeline wiring
29. `suggest_tags` uses `config["haiku_model"]`; prompt contains every vocabulary entry passed in.
30. Malformed JSON response → `[]`, logs warning, does not raise.
31. Markdown-fenced JSON response parses correctly.
32. `_call_api` raising inside `suggest_tags` still lets `process_file` complete: wiki written, raw file deleted, ledger records success.
33. **CREATE path:** new page's first line is `---`, frontmatter contains suggested tags.
34. **Merge path (5a):** existing `tags: [alpha]` + note suggesting `beta` → both present, `alpha` first.
35. **Merge path, LLM dropped frontmatter:** existing `category: Work` + `tags: [alpha]`, synthesis returns body-only → written file still has both `category: Work` and `alpha`.
36. **Splice path (5b):** page over `large_page_threshold_chars` still gains tags in frontmatter; unspliced `## ` sections byte-identical.
37. `NO_CHANGES` splice response with no new tags → file byte-identical.
38. **Daily skip:** date-stem note routed to `daily_folder` → zero `suggest_tags` calls, no frontmatter on the written page.
39. `Daily-Log.md` (wiki root, non-date stem) IS tagged.
40. **Rollback:** `tags_enabled=False` → `messages.create` call count and written content unchanged from today.

### 6.6 Non-regression
41. Full existing `test_organizer.py` passes unchanged, including `test_wiki_create_for_new_topic`'s exact-string assertion — `synthesize_wiki` itself stays untouched; all tag writing happens in `process_file`.
42. `test_mcp_server.py`, `test_migrate_daily_pages.py` pass unchanged.
43. `sanitize_topic_name`, `_normalize_title`, `find_similar_page`, `_looks_like_session_title`, `_is_daily_note`, `_daily_note_route`, `_defuse_unknown_wikilinks`, `_group_files_by_shared_pages`, `_call_api` all unmodified. `_normalize_tag` is a new wrapper, not an edit to `_normalize_title` (which `consolidate_wiki.py`/`mcp_server.py` depend on).

### 6.7 Live-vault outcome (after one supervised run)
44. Every page the run wrote has valid frontmatter with a non-empty `tags:` block.
45. No page lost a pre-existing tag or non-tag frontmatter key.
46. Fenced-page count stays at 3 — none added.
47. Distinct-tag count grows by at most (notes processed × `tag_max_new_per_note`).
48. Catalog entries carry a `tags` key; no `summary` starts with a frontmatter key.

---

## 7. Composition with the two companion specs

| Interaction | Resolution |
|---|---|
| Wikilink spec's §4.1 (`utf-8-sig`) | Composes cleanly; `_find_frontmatter` must still tolerate `\ufeff` since `process_file`'s Phase 1 read is plain `utf-8`. |
| Robustness spec's §4.2 (`_CATALOG_PARSER_VERSION`) | §2.3 requires a bump; whichever spec lands second bumps to the next version. |
| Robustness spec's §5.3 catalog tests | Land those first; they're the regression net for §2.2's summary-scan change. |
| Robustness spec's §4 `_CONFIG_DEFAULTS` | §5's five keys go there if it has shipped. |
| Robustness spec's §6.3 run-summary counters | Add `tags_added` and `tags_new_vocabulary` — the sprawl-observability handle. |
| Wikilink spec's §4.3/§4.4 router visibility | Convergence depends on it — 209/269 pages are currently unreachable, so untagged pages that never get written never get tags. Ship router visibility first if choosing an order. |

---

## 8. Scope boundaries — explicitly OUT

1. **Retroactively tagging the 225 untagged pages.** This spec delivers "adds tags going forward," exactly as asked. A one-time backfill (269 LLM calls, 269 files rewritten in one pass, no per-page review) is a separate, higher-risk decision — flag to Brian, don't build here.
2. **Repairing the 3 fence-wrapped pages.** Pipeline detects and skips them (criterion 19).
3. **Repairing mid-file embedded frontmatter** in `NEXUS.md`/`Obsidian.md`/`Sales.md`. Legacy merge damage; manual.
4. **The 33 body-level inline `#hashtag` lines.** Not read into the vocabulary, not written, not removed — mixing a prose convention into a structured field is a separate call, and that corpus has real casing duplicates this spec has no mandate to arbitrate.
5. **`wiki/processed/` and `wiki/daily/`.** Outside the catalog's deliberate non-recursive glob. Do not widen it.
6. **Any other frontmatter key** (`category`, `date`, `updated`, `aliases`, `related`). §3.4 preserves them; never authors them.
7. **A `_meta/tag-registry.json`.** The catalog already carries it for free (§1.1).
8. **A YAML dependency.** Stdlib-only per the module's existing constraint; a 3-form tag parser is ~40 lines, and `pyyaml` would reject the 4 hash-string pages' intent outright.
9. **Changing `synthesize_wiki`'s signature, return value, or prompts.** All tag work lives in `process_file`.
10. **Tagging raw notes / changing `POST /raw` / a frontmatter contract on inbound notes.** Rejected by the robustness spec's §9.5 for reasons that still hold.
11. **`consolidate_wiki.py`, `migrate_daily_pages.py`, anything under `backend/`.**
12. **Obsidian-side tag panes/MOC pages.** Writing the field is the ask; Obsidian indexes it natively.

---

## 9. Suggested implementation order

1. §2.1 `_find_frontmatter` + `_parse_frontmatter_tags` + criteria 1–7.
2. §2.2 summary fix + §2.3 version bump + criteria 9–11.
3. §3.1–§3.2 `_write_frontmatter_tags` + criteria 13–20.
4. §1.3 `_normalize_tag` + `_reconcile_tags` + criteria 21–28.
5. §4 `suggest_tags` + §2.4 vocabulary + criteria 29–31.
6. §3.3 + §3.4 pipeline wiring + criteria 32–40. Land `tags_enabled=false` first, confirm criterion 40, then flip to `true`.
7. Full suite green, one supervised manual run against a vault copy, then §6.7 measured against the live run.

---

### Critical Files for Implementation

- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\brain_organizer.py` — `:265` (`_extract_page_entry`), `:293` (prose scan start), `:326`/`:352`/`:368` (catalog + parser version), `:684` (`route_topics` pattern to mirror), `:1220` (Phase 0), `:1231` (Phase 1), `:1245`–`:1253` (Phase 2)
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\tests\test_organizer.py` — 48 new tests; `test_wiki_create_for_new_topic` is the regression anchor
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\tests\conftest.py` — fixtures to reuse; add the 5 config keys
- `C:\Users\Brian\Documents\Agentic os\nexus\modules\brain-organizer\config.json` — the 5 new keys
- `C:\Users\Brian\iCloudDrive\iCloud~md~obsidian\Brain\_meta\wiki-catalog.json` — read-only ground truth
