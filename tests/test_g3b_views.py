"""G3b phase 5, rule 3: nothing drops off the digest (plan §6)."""
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

import backend.database as bd
from backend.agents import g3b, outcomes
from test_g3b_grouping import _parent, _settings, db, on  # noqa: F401
from test_g3b_send import _child


def _singles(n):
    with bd.engine.begin() as c:
        for i in range(n):
            c.execute(text(
                "INSERT INTO outcomeflag (source, \"check\", fingerprint, summary, severity, status, surfaced_count, last_surfaced_at,"
                " created_at, updated_at, suppressed) VALUES ('vault_signals', :ck, :fp, 'noise', 'low', 'open', 1, :t, :t, :t, 0)"),
                {"ck": f"n{i}", "fp": f"vault_signals:n{i}", "t": g3b.db_ts(datetime.utcnow())})


async def test_unpaged_grouped_child_listed_beyond_every_cap(on):
    p = _parent()
    kid = _child(parent=p)  # oldest rows...
    _singles(1100)          # ...buried under more than 1000 newer singles
    unpaged = await outcomes.unpaged_grouped()
    assert [u["id"] for u in unpaged] == [kid]
    from backend.agents import briefing

    section = briefing._build_open_items_section(await outcomes.open_flags(limit=20), unpaged)
    assert f"#{kid} " in section and f"(under #{p})" in section
    assert section.count(f"#{kid} ") == 1
    from backend.agents import tools

    out = await tools._open_flags({})
    assert out.startswith("grouped, not yet paged: 1") and f"#{kid} " in out and len(out) <= tools.MAX_TOOL_RESULT_CHARS
    rows, total = await outcomes.open_flags_page(50, 23)
    assert total == 1100 + 2 and rows  # page 23 reaches rows past 1000


async def test_paged_child_not_in_unpaged_section_but_marked(on):
    p = _parent()
    kid = _child(parent=p, paged=True)
    assert await outcomes.unpaged_grouped() == []
    rows = await outcomes.open_flags(limit=50)
    assert outcomes.group_suffix(next(r for r in rows if r["id"] == kid)) == f" (under #{p})"


async def test_off_views_unchanged(db, monkeypatch):
    from backend.agents import briefing

    _settings(monkeypatch, corr="false")
    _singles(3)
    flags = await outcomes.open_flags()
    assert await outcomes.unpaged_grouped() == []
    assert briefing._build_open_items_section(flags, []) == briefing._build_open_items_section(flags)
    assert "(under" not in briefing._build_open_items_section(flags)


async def test_evidence_lists_grouped_children(on):
    from backend.agents import incident_diag

    p = _parent()
    kid = _child(parent=p)
    s = type("S", (), {"graph_enabled": True, "graph_collect_minutes": 60})()
    with patch("backend.config.get_settings", return_value=s):
        ev = await incident_diag._evidence("unraid_array", datetime.utcnow())
    assert f"Grouped under this incident: #{kid} homelab_watch:docker:demo-app" in ev
