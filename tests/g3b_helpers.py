"""Plain helpers shared by the G3b test files (fixtures live in conftest.py: g3b_db, g3b_on)."""
from datetime import datetime, timedelta

from sqlalchemy import text

import backend.database as bd
from backend.agents import g3b


def _settings(monkeypatch, graph="true", corr="true"):
    import backend.config as config_mod

    monkeypatch.setenv("GRAPH_ENABLED", graph)
    monkeypatch.setenv("GRAPH_CORRELATION_ENABLED", corr)
    monkeypatch.setattr(config_mod, "_settings_instance", None)


def _parent(paged=True, suppressed=False, status="open", age_min=1):
    with bd.engine.begin() as c:
        c.execute(text(
            "INSERT INTO outcomeflag (source, \"check\", fingerprint, summary, severity, status, surfaced_count, "
            "last_surfaced_at, created_at, updated_at, suppressed, paged_at) VALUES ('homelab_watch', 'unraid_array', "
            "'homelab_watch:unraid_array', 'array down', 'high', :st, 1, :t, :t, :t, :sup, :p)"),
            {"st": status, "t": g3b.db_ts(datetime.utcnow() - timedelta(minutes=age_min)), "sup": int(suppressed),
             "p": g3b.db_ts(datetime.utcnow()) if paged else None})
        return c.execute(text("SELECT max(id) FROM outcomeflag")).scalar_one()


def _row(fid):
    with bd.engine.connect() as c:
        return c.execute(text("SELECT parent_flag_id, attach_op, paged_at, surfaced_count FROM outcomeflag WHERE id = :i"),
                         {"i": fid}).one()


def _child(parent=None, paged=False, surfacing=1, released_from=None, status="open"):
    with bd.engine.begin() as c:
        c.execute(text(
            "INSERT INTO outcomeflag (source, \"check\", fingerprint, summary, severity, status, surfaced_count, last_surfaced_at,"
            " created_at, updated_at, suppressed, parent_flag_id, released_from_parent_id, paged_at) VALUES ('homelab_watch',"
            " 'docker:demo-app', 'homelab_watch:docker:demo-app', 'stopped', 'medium', :st, :s, :t, :t, :t, 0, :p, :r, :pg)"),
            {"st": status, "s": surfacing, "t": g3b.db_ts(datetime.utcnow()), "p": parent, "r": released_from,
             "pg": g3b.db_ts(datetime.utcnow()) if paged else None})
        return c.execute(text("SELECT max(id) FROM outcomeflag")).scalar_one()


def _cols(fid):
    with bd.engine.connect() as c:
        return c.execute(text("SELECT paged_at, send_claim_id, send_attempts FROM outcomeflag WHERE id = :i"), {"i": fid}).one()


def _payload(fid, s=1, kind="homelab_docker_stopped"):
    return {"type": kind, "content": "x", "flag_id": fid, "flag_surfacing": s}
