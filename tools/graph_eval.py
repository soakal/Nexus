"""Phase 0 of the knowledge-graph plan: score today's vault search on real questions.

Backends (each run raw and as keywords, since production callers send LLM-written queries):
  nexus / nexus-kw   GET /api/vault/search (obsidian.vault_search, the chat/orchestrator tool)
  carl  / carl-kw    vault_search_notes over Carl's MCP endpoint (defaults: only query + limit sent)

Keys come from the environment; the question file path from GRAPH_EVAL_QUESTIONS:
  secret run NEXUS_API_KEY CARL_MCP_TOKEN -- python3 tools/graph_eval.py --out DIR
Question file: a JSON list of {id, kind, q, gold_sets, source, known_bad, scored[, exclusion_reason]}.

Each question has gold_sets: alternative answer sets. [[A],[B]] means A or B answers it;
[[A,B]] means both are needed (multihop). complete@k = some set entirely in the top k.
Questions with scored:false are listed but never counted. Any backend error marks the run
INVALID instead of quietly counting as a miss.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# The question set names private notes (health, logins), so it lives outside this public repo.
QUESTIONS = Path(os.environ.get("GRAPH_EVAL_QUESTIONS", "~/repos/.graph-plan-2026-10-30/phase0/questions.json")).expanduser()
NEXUS_URL = os.environ.get("NEXUS_URL", "http://192.168.1.62:8000")
CARL_URL = os.environ.get("CARL_URL", "http://100.84.21.43:3100/mcp")
KINDS = ("lookup", "multihop", "temporal", "bad_today")
TOP = 10
KS = (1, 5, 10)

# Both backends print each hit as a line starting with **path.md**; anchored to line start
# so a bold path inside a context snippet (which never starts a line) is not taken as a hit.
_HIT_LINE = re.compile(r"^\*\*([^*\n]+?\.md)\*\*(?: \(score:\d+\))?\s*$", re.M)
_STOP = set("a an and are at by did do does for from has how i if in is it its my of on or the "
            "to was what when where which who why with".split())


def keywords(q: str) -> str:
    return " ".join(w for w in re.findall(r"[\w.\-@]+", q.lower()) if w not in _STOP)


def norm(path: str) -> str:
    """'Brain/wiki/X.md' and 'wiki/X.md' -> 'wiki/X'."""
    path = path.strip()
    if path.startswith("Brain/"):
        path = path[len("Brain/"):]
    return path[:-3] if path.endswith(".md") else path


def parse_results(text: str) -> list[str]:
    out: list[str] = []
    for m in _HIT_LINE.findall(text):
        p = norm(m)
        if p not in out:  # a duplicate must not use up a rank
            out.append(p)
    return out


def score(ranked: list[str], gold_sets: list[list[str]]) -> dict:
    s = {}
    for k in KS:
        top = set(ranked[:k])
        s[f"complete@{k}"] = any(set(g) <= top for g in gold_sets)
        s[f"any@{k}"] = any(top & set(g) for g in gold_sets)
    gold_any = {p for g in gold_sets for p in g}
    s["first_rank"] = next((i + 1 for i, p in enumerate(ranked) if p in gold_any), None)
    return s


def _post(url: str, body: dict, headers: dict) -> tuple[dict, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw, hdrs = r.read().decode(), dict(r.headers)
    if not raw.strip():
        return {}, hdrs
    if raw.lstrip().startswith("{"):
        return json.loads(raw), hdrs
    # Assumes one JSON-RPC response per request, sent as the last SSE data: line (true for Carl).
    data = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    return (json.loads(data[-1]) if data else {}), hdrs


def nexus_search(q: str) -> list[str]:
    url = f"{NEXUS_URL}/api/vault/search?" + urllib.parse.urlencode({"q": q, "max_results": TOP})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['NEXUS_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.load(r)["result"]
    if result.startswith(("Vault search unavailable", "Obsidian vault not found")):
        raise RuntimeError(result[:200])
    return parse_results(result)


class Carl:
    def __init__(self) -> None:
        self.auth = {"Authorization": f"Bearer {os.environ['CARL_MCP_TOKEN']}"}
        _, hdrs = _post(CARL_URL, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "graph_eval", "version": "0"}}}, self.auth)
        sid = next((v for k, v in hdrs.items() if k.lower() == "mcp-session-id"), None)
        if sid:
            self.auth["Mcp-Session-Id"] = sid
        _post(CARL_URL, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.auth)
        self.n = 1

    def search(self, q: str) -> list[str]:
        self.n += 1
        resp, _ = _post(CARL_URL, {"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {
            "name": "vault_search_notes", "arguments": {"query": q, "limit": TOP}}}, self.auth)
        if "error" in resp:
            raise RuntimeError(f"carl rpc error: {resp['error']}")
        result = resp.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise RuntimeError(f"carl malformed response: {str(resp)[:200]}")  # never a silent empty result
        text = "".join(c.get("text", "") for c in result["content"])
        if result.get("isError"):
            raise RuntimeError(f"carl tool error: {text[:200]}")
        return parse_results(text)


def run(questions: list[dict], backends: dict) -> list[dict]:
    rows = []
    for q in questions:
        for name, (fn, kw) in backends.items():
            query = keywords(q["q"]) if kw else q["q"]
            row = {"id": q["id"], "backend": name, "query": query}
            try:
                row["ranked"] = fn(query)
                row.update(score(row["ranked"], q["gold_sets"]) if q["scored"] else {})
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
            rows.append(row)
    return rows


def report(questions: list[dict], rows: list[dict]) -> str:
    byid = {q["id"]: q for q in questions}
    errors = [r for r in rows if "error" in r]
    out = [f"**Run {'INVALID' if errors else 'valid'}**: {len(errors)} backend error(s)."]
    out += [f"- ERR {r['backend']} Q{r['id']}: {r['error']}" for r in errors]
    out += ["", "| backend | kind | n | complete@1 | complete@5 | complete@10 | any@5 |", "|---|---|---|---|---|---|---|"]
    backends = list(dict.fromkeys(r["backend"] for r in rows))
    for b in backends:
        for kind in (*KINDS, "all"):
            sel = [r for r in rows if r["backend"] == b and "error" not in r and byid[r["id"]]["scored"]
                   and (kind == "all" or byid[r["id"]]["kind"] == kind)]
            if not sel:
                continue
            f = lambda key: f"{sum(r[key] for r in sel)}/{len(sel)}"  # noqa: E731
            out.append(f"| {b} | {kind} | {len(sel)} | {f('complete@1')} | {f('complete@5')} | {f('complete@10')} | {f('any@5')} |")
    out += ["", "Per question: first gold rank / complete@10 (✓) — `x` = not scored",
            "", "| id | kind | question | " + " | ".join(backends) + " |", "|---|---|---|" + "---|" * len(backends)]
    for q in questions:
        cells = []
        for b in backends:
            r = next(r for r in rows if r["id"] == q["id"] and r["backend"] == b)
            if "error" in r:
                cells.append("ERR")
            elif not q["scored"]:
                cells.append("x")
            else:
                cells.append(f"{r['first_rank'] or 'miss'}{' ✓' if r['complete@10'] else ''}")
        out.append(f"| {q['id']} | {q['kind']} | {q['q']} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def _manifest(vault: Path) -> str:
    """Hash of every note's path, size and mtime: tells two runs apart if the corpus changed."""
    h = hashlib.sha256()
    for p in sorted(vault.rglob("*.md")):
        st = p.stat()
        h.update(f"{p.relative_to(vault)}\0{st.st_size}\0{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def validate(questions: list[dict], vault: Path | None = None) -> None:
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "duplicate ids"
    for q in questions:
        assert q["kind"] in KINDS and q["q"] and q.get("source"), q
        if q["scored"]:
            assert q["gold_sets"] and all(q["gold_sets"]), f"Q{q['id']}: empty gold set"
        else:
            assert q.get("exclusion_reason"), f"Q{q['id']}: unscored without a reason"
        if q["kind"] == "multihop" and q["scored"]:
            assert all(len(set(g)) >= 2 for g in q["gold_sets"]), f"Q{q['id']}: multihop needs two different notes"
        if vault:
            missing = [g for s in q["gold_sets"] for g in s if not (vault / f"{g}.md").exists()]
            assert not missing, f"Q{q['id']}: gold not in vault: {missing}"
    scored = [q for q in questions if q["scored"]]
    assert sum(q["kind"] == "multihop" for q in scored) >= 8, "need >= 8 scored multihop"


def _selftest() -> None:
    text = ("**Brain/wiki/Unraid.md**\nsee **wiki/Fake.md** inline\n\n**wiki/Homelab.md** (score:38)\n"
            "...\n**wiki/Unraid.md** (score:2)\n**raw/x.md**")
    assert parse_results(text) == ["wiki/Unraid", "wiki/Homelab", "raw/x"], parse_results(text)
    ranked = ["raw/x", "wiki/A", "wiki/C"]
    s = score(ranked, [["wiki/A"], ["wiki/B"]])  # alternatives: either is enough
    assert s["complete@5"] and not s["complete@1"] and s["first_rank"] == 2, s
    s = score(ranked, [["wiki/A", "wiki/B"]])  # joint: partial evidence is not complete
    assert s["any@5"] and not s["complete@10"], s
    assert score(ranked, [["wiki/A", "wiki/C"]])["complete@5"]
    assert score([], [["wiki/A"]])["first_rank"] is None
    assert keywords("What is the backup target?") == "backup target"
    rows = run([{"id": 1, "q": "x", "kind": "lookup", "gold_sets": [["wiki/A"]], "scored": True, "source": "t"}],
               {"boom": (lambda q: (_ for _ in ()).throw(RuntimeError("down")), False)})
    assert "error" in rows[0] and "complete@5" not in rows[0]
    assert report([{"id": 1, "q": "x", "kind": "lookup", "scored": True}], rows).startswith("**Run INVALID**")
    carl = Carl.__new__(Carl)
    carl.n, carl.auth = 1, {}
    for bad in ({}, {"result": {}}, {"result": {"isError": True, "content": [{"text": "no"}]}}):
        globals()["_post"], real = (lambda *a, b=bad: (b, {})), _post
        try:
            carl.search("x")
            raise AssertionError(f"accepted {bad}")
        except RuntimeError:
            pass
        finally:
            globals()["_post"] = real
    excl = [{"id": 1, "q": "x", "kind": "bad_today", "gold_sets": [], "scored": False, "source": "t"}]
    assert "complete@5" not in run(excl, {"ok": (lambda q: ["wiki/A"], False)})[0]
    mh = [{"id": i, "q": "x", "kind": "multihop", "gold_sets": [["wiki/A", "wiki/A"]], "scored": True, "source": "t"}
          for i in range(8)]
    try:
        validate(mh)
        raise AssertionError("accepted a joint set with one repeated note")
    except AssertionError as e:
        assert "two different notes" in str(e), e
    qs = json.loads(QUESTIONS.read_text())
    brain = Path.home() / "Brain"
    validate(qs, brain if brain.is_dir() else None)
    print(f"selftest ok ({len(qs)} questions, {sum(q['scored'] for q in qs)} scored)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backend", choices=["nexus", "carl", "all"], default="all")
    ap.add_argument("--out", type=Path, help="directory for results-<ts>.json and report-<ts>.md")
    ap.add_argument("--selftest", action="store_true", help="check parsing/scoring/questions, no network")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    raw = QUESTIONS.read_bytes()
    questions = json.loads(raw)
    vault = Path(os.environ.get("GRAPH_EVAL_VAULT", "~/Brain")).expanduser()
    validate(questions, vault if vault.is_dir() else None)
    snapshot = {"questions_sha256": hashlib.sha256(raw).hexdigest(), "vault": str(vault),
                "vault_manifest_sha256": _manifest(vault) if vault.is_dir() else None}
    backends = {}
    if args.backend in ("nexus", "all"):
        backends |= {"nexus": (nexus_search, False), "nexus-kw": (nexus_search, True)}
    if args.backend in ("carl", "all"):
        carl = Carl()
        backends |= {"carl": (carl.search, False), "carl-kw": (carl.search, True)}
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rows = run(questions, backends)
    text = report(questions, rows)
    print(text)
    if args.out:
        ts = time.strftime("%Y%m%d-%H%M%S")
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"results-{ts}.json").write_text(json.dumps(
            {"started": started, "nexus_url": NEXUS_URL, "carl_url": CARL_URL, "top": TOP, **snapshot,
             "questions": questions, "rows": rows}, indent=1))
        (args.out / f"report-{ts}.md").write_text(text + "\n")
    sys.exit(1 if any("error" in r for r in rows) else 0)


if __name__ == "__main__":
    main()
