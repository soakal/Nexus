"""Vault link index (plan-v3 §3.3/§4.1): Brain/ notes -> graph note/tag nodes, links_to/tagged edges.

`documents` edges are NOT written here (single owner = reconcile_documents, a later commit).
"""
from __future__ import annotations

import hashlib
import os
import re
import stat as _stat
import time
from dataclasses import dataclass, field
from pathlib import Path

import backend.api.vault as _vault_api
from backend.graph import now_iso, store
from backend.graph.store import Edge, Node
from backend.integrations import obsidian

WIKILINK_PAT = _vault_api.WIKILINK_PAT
_TAG_INLINE = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]*)")  # headings fail (space after #), URL fragments fail (no space before #)
_FM_KEYS = frozenset({"title", "aliases", "tags", "category", "type", "status", "host", "vmid", "service"})
_LIST_KEYS = ("aliases", "tags")


def _brain_root() -> Path:
    return _vault_api._brain_root()  # module-qualified so tests' monkeypatch applies


def _unquote(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}  # unterminated block: not frontmatter
    fm: dict[str, str | list[str]] = {}
    key: str | None = None
    for line in lines[1:end]:
        s = line.strip()
        if s.startswith("- ") or s == "-":
            if key is not None and isinstance(fm.get(key), list):
                fm[key].append(_unquote(s[1:]))  # type: ignore[union-attr]
            continue
        k, sep, v = line.partition(":")
        key = k.strip().lower() if sep and not line[:1].isspace() else None
        if key is None or key not in _FM_KEYS:
            key = None
            continue
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            fm[key] = [_unquote(x) for x in v[1:-1].split(",") if x.strip()]
        elif v == "" and key in _LIST_KEYS:
            fm[key] = []  # block list follows
        else:
            fm[key] = _unquote(v)
    return fm


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [x for x in v if x]
    return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else []


def parse_links(text: str) -> list[str]:
    return [m.group(1).strip() for m in WIKILINK_PAT.finditer(text)]


def parse_tags(fm: dict, text: str) -> set[str]:
    tags = {t.lstrip("#").strip().lower() for t in _as_list(fm.get("tags"))}
    tags |= {m.group(1).lower() for m in _TAG_INLINE.finditer(text)}
    return {t for t in tags if t}


class Resolver:
    """Obsidian-like link resolution; never guesses (unknown -> (None, False))."""

    def __init__(self, notes: dict[str, dict]):
        self._path = {i: n["path"] for i, n in notes.items()}
        self._rungs: list[dict[str, set[str]]] = [{} for _ in range(6)]
        for i, n in notes.items():
            rel = n["path"][:-3] if n["path"].endswith(".md") else n["path"]
            stem, title = n["stem"], n.get("title") or ""
            keys = [rel, stem, title, None, stem.lower(), title.lower()]
            for rung, k in zip(self._rungs, keys, strict=True):
                if k:
                    rung.setdefault(k, set()).add(i)
            for a in n.get("aliases") or []:
                self._rungs[3].setdefault(a, set()).add(i)

    def resolve(self, target: str) -> tuple[str | None, bool]:
        t = target.strip()
        rel = t[:-3] if t.endswith(".md") else t
        for rung, key in zip(self._rungs, [rel, t, t, t, t.lower(), t.lower()], strict=True):
            ids = rung.get(key)
            if ids:
                best = min(ids, key=lambda i: (len(self._path[i]), self._path[i]))
                return best, len(ids) > 1
        return None, False


@dataclass
class Scan:
    notes: dict[str, tuple[int, int]] = field(default_factory=dict)  # id -> (mtime_ns, size)
    unreadable: set[str] = field(default_factory=set)
    skipped_oversize: int = 0


def scan(root: Path) -> Scan:
    """Listing errors RAISE (a vanished vault must not look like mass deletion); a failing stat() -> unreadable."""
    out = Scan()
    stack = [str(root)]
    while stack:
        with os.scandir(stack.pop()) as it:
            entries = list(it)
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if e.name.lower() not in obsidian._EXCLUDED_DIRS:
                    stack.append(e.path)
                continue
            if not e.name.endswith(".md"):
                continue
            nid = "note:" + Path(e.path).relative_to(root).as_posix()[:-3]
            try:
                st = e.stat()
            except OSError:
                out.unreadable.add(nid)
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            if st.st_size > obsidian.MAX_NOTE_BYTES:
                out.skipped_oversize += 1
                continue
            out.notes[nid] = (st.st_mtime_ns, st.st_size)
    return out


def _read_note(root: Path, nid: str, fp: tuple[int, int]) -> dict:
    """Raises OSError if unreadable."""
    rel = nid[5:] + ".md"
    raw = (root / rel).read_bytes()
    text = raw.decode("utf-8", errors="ignore")
    fm = parse_frontmatter(text)
    stem = rel.rsplit("/", 1)[-1][:-3]
    attrs: dict = {
        "path": rel, "title": str(fm.get("title") or stem), "aliases": _as_list(fm.get("aliases")),
        "sha256": hashlib.sha256(raw).hexdigest(), "mtime_ns": fp[0], "size": fp[1],
    }
    for k in ("category", "type", "status", "host", "vmid", "service"):
        if fm.get(k):
            attrs[k] = str(fm[k])
    return {"attrs": attrs, "stem": stem, "links": parse_links(text), "tags": parse_tags(fm, text)}


def refresh(full: bool = False) -> dict:
    t0 = time.monotonic()
    seen_at = now_iso()
    try:
        return _refresh(full, seen_at, t0)
    except Exception as e:
        try:
            store.record_job("vault_index", ok=False, error=str(e), at=now_iso())
        except Exception:  # noqa: S110 - never mask the original failure
            pass
        raise


def _refresh(full: bool, seen_at: str, t0: float) -> dict:
    root = _brain_root()
    sc = scan(root)
    stored = {n["id"]: n["attrs"] or {} for n in store.list_nodes(types=("note",))}
    removed = {i for i in stored if i not in sc.notes and i not in sc.unreadable}
    unreadable = set(sc.unreadable)
    parsed: dict[str, dict] = {}

    def read(ids) -> None:
        for nid in ids:
            if nid in parsed:
                continue
            try:
                parsed[nid] = _read_note(root, nid, sc.notes[nid])
            except OSError:
                unreadable.add(nid)  # leave stored node/edges/fingerprint untouched; retried next tick

    changed = [
        i for i, fp in sc.notes.items()
        if full or i not in stored or (stored[i].get("mtime_ns"), stored[i].get("size")) != fp
    ]
    read(changed)
    n_changed = len(parsed)
    # Events that can change what a link in an UNCHANGED note points at -> re-resolve everything.
    if not full and (
        removed
        or any(i not in stored for i in parsed)
        or any(
            (a["title"], a["aliases"]) != (stored[i].get("title"), stored[i].get("aliases"))
            for i, a in ((i, p["attrs"]) for i, p in parsed.items()) if i in stored
        )
    ):
        full = True
        read(sc.notes)

    table: dict[str, dict] = {}
    for i in sc.notes:
        if i in parsed:
            table[i] = {**parsed[i]["attrs"], "stem": parsed[i]["stem"]}
        elif i in stored:  # unreadable or unchanged-and-not-re-read: stored title/aliases keep links to it resolving
            table[i] = {**stored[i], "stem": i.rsplit("/", 1)[-1]}
    for i in unreadable & stored.keys() - table.keys():
        table[i] = {**stored[i], "stem": i.rsplit("/", 1)[-1]}
    resolver = Resolver(table)

    nodes, tag_nodes, edges = [], {}, []
    unresolved_total = ambiguous_total = 0
    for nid, p in parsed.items():
        unresolved = ambiguous = 0
        targets: set[str] = set()
        for link in dict.fromkeys(p["links"]):
            dest, amb = resolver.resolve(link)
            ambiguous += amb
            if dest is None:
                unresolved += 1
            elif dest != nid:
                targets.add(dest)
        sha = p["attrs"]["sha256"]
        edges += [Edge(nid, d, "links_to", "wikilink", sha) for d in sorted(targets)]
        for t in sorted(p["tags"]):
            tag_nodes[f"tag:{t}"] = Node(f"tag:{t}", "tag", t, source="vault")
            edges.append(Edge(nid, f"tag:{t}", "tagged", "frontmatter", sha))
        attrs = {**p["attrs"], "unresolved": unresolved, "ambiguous": ambiguous}
        nodes.append(Node(nid, "note", attrs["title"], tuple(attrs["aliases"]), attrs, "vault"))
        unresolved_total += unresolved
        ambiguous_total += ambiguous

    result = {
        "scanned": len(sc.notes), "changed": n_changed, "removed": len(removed),
        "links": sum(e.type == "links_to" for e in edges), "unresolved": unresolved_total,
        "ambiguous": ambiguous_total, "skipped_oversize": sc.skipped_oversize, "unreadable": len(unreadable),
    }
    with store.batch() as conn:  # one transaction: any failure rolls the whole run back
        for nid in parsed:
            store.delete_edges(src=nid, source=("wikilink", "frontmatter"), conn=conn)
        store.upsert_nodes(nodes, seen_at, conn, merge_attrs=False)
        store.upsert_nodes(tag_nodes.values(), seen_at, conn, merge_attrs=False)
        store.upsert_edges(edges, seen_at, conn)
        store.delete_nodes(removed, conn)
        store.delete_orphan_tags(conn)
        result["seconds"] = round(time.monotonic() - t0, 3)
        store.record_job("vault_index", ok=True, stats=result, at=seen_at, conn=conn)
    return result
