"""G3b phase 1: outcomeflag columns migration, the correlation switch, the flag_released kind."""
from sqlalchemy import text
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

import backend.database as bd

# outcomeflag as production has it before G3b (21 columns incl. the calibration pair) + the partial unique index
PRE_G3B_DDL = """
CREATE TABLE outcomeflag (
  id INTEGER NOT NULL PRIMARY KEY, source VARCHAR NOT NULL, "check" VARCHAR NOT NULL, fingerprint VARCHAR NOT NULL,
  summary VARCHAR NOT NULL, detail VARCHAR, severity VARCHAR NOT NULL, status VARCHAR NOT NULL,
  resolved_at DATETIME, resolved_by VARCHAR, resolution_note VARCHAR, deferred_until DATETIME,
  action_log_id INTEGER, surfaced_count INTEGER NOT NULL, last_surfaced_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, suppressed BOOLEAN DEFAULT 0, suppressed_reason VARCHAR
)"""


def _old_engine(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with e.begin() as c:
        c.execute(text(PRE_G3B_DDL))
        c.execute(text("CREATE UNIQUE INDEX ux_outcomeflag_open ON outcomeflag(fingerprint) WHERE status = 'open' AND fingerprint != ''"))
        for i, status in ((1, "open"), (2, "resolved"), (3, "open")):
            c.execute(text(
                "INSERT INTO outcomeflag VALUES (:i, 'homelab_watch', :ck, :fp, 's', NULL, 'medium', :st, NULL, NULL, NULL, NULL,"
                " NULL, 1, '2026-11-01 00:00:00', '2026-11-01 00:00:00', '2026-11-01 00:00:00', :sup, NULL)"),
                {"i": i, "ck": f"vm:{100 + i}", "fp": f"homelab_watch:vm:{100 + i}", "st": status, "sup": int(i == 3)})
    monkeypatch.setattr(bd, "engine", e)
    return e


def test_migration_from_pre_g3b_schema(monkeypatch):
    e = _old_engine(monkeypatch)
    assert set(bd.missing_g3b_columns()) == set(bd.G3B_OUTCOMEFLAG_COLUMNS) | {"ix_outcomeflag_parent_flag_id"}
    bd._ensure_outcomeflag_columns()
    assert bd.missing_g3b_columns() == []
    bd._ensure_outcomeflag_columns()  # idempotent
    assert bd.missing_g3b_columns() == []
    with Session(e) as s:
        rows = {r.id: r for r in s.query(bd.OutcomeFlag).all()}
        assert sorted(rows) == [1, 2, 3]  # rows retained
        assert rows[2].status == "resolved" and rows[3].suppressed is True
        r = s.get(bd.OutcomeFlag, 1)  # the ORM selects every mapped column: this is what breaks if one is missing
        assert r.parent_flag_id is None and r.paged_at is None and r.send_attempts is None and r.attach_op is None


def test_missing_columns_logged(monkeypatch, caplog):
    _old_engine(monkeypatch)
    monkeypatch.setattr(bd, "_safe_add_column", lambda *a, **k: None)  # simulate a migration that silently did nothing
    with caplog.at_level("ERROR", logger="backend.database"):
        bd._ensure_outcomeflag_columns()
    assert "outcomeflag migration incomplete" in caplog.text


def test_correlation_switch_defaults_off_and_needs_graph(monkeypatch):
    import backend.config as config_mod
    from backend.config import Settings
    from backend.graph import correlate

    assert Settings.model_fields["graph_correlation_enabled"].default is False
    for graph, corr, want in (("true", "true", True), ("false", "true", False), ("true", "false", False)):
        monkeypatch.setenv("GRAPH_ENABLED", graph)
        monkeypatch.setenv("GRAPH_CORRELATION_ENABLED", corr)
        monkeypatch.setattr(config_mod, "_settings_instance", None)
        assert correlate.enabled() is want
    # read once: flipping env without resetting the cached settings changes nothing (rule 4: a restart is required)
    monkeypatch.setenv("GRAPH_CORRELATION_ENABLED", "true")
    assert correlate.enabled() is False
    monkeypatch.setattr(config_mod, "_settings_instance", None)


def test_graph_flags_never_group():
    from backend.graph import correlate

    assert correlate.node_for_flag("graph", "stale:collect") is None
    assert ("homelab_watch", "unraid_array") in correlate.PARENT_CHECKS and len(correlate.PARENT_CHECKS) == 1


def test_flag_released_registered_and_never_mutable():
    import pytest

    from backend.events import NOTIFY_KINDS
    from backend.safety import governor

    assert "flag_released" in NOTIFY_KINDS and "flag_released" in governor._NEVER_MUTABLE_NOTIFY_KINDS
    with pytest.raises(ValueError):
        governor.add_muted_notify_kind("flag_released")
