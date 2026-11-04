from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents import incident_diag


@pytest.fixture(autouse=True)
def _reset():
    incident_diag.reset()
    yield
    incident_diag.reset()


def _settings(**overrides):
    s = MagicMock()
    s.incident_diag_enabled = True
    s.graph_enabled = False  # evidence block has its own tests below
    s.graph_collect_minutes = 60
    s.orchestrator_executor_model = "claude-sonnet-4-6"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.mark.asyncio
async def test_maybe_diagnose_fires_on_vm_key():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})
        await incident_diag._diag_task

    run_mock.assert_awaited_once()
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "incident_diagnosis"
    assert notify_mock.await_args.kwargs["buttons"] == [{"text": "▶ Start", "callback_data": "vm:start:100"}]


@pytest.mark.asyncio
async def test_maybe_diagnose_ignores_garage_and_temp_keys():
    run_mock = AsyncMock(return_value="diagnosis text")
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock):
        incident_diag.maybe_diagnose({
            "vms": [], "docker": [], "array": [], "backups": [],
            "disk_temps": ["disk:sda"], "garage": ["garage_open"],
        })

    assert incident_diag._diag_task is None
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_diagnose_respects_per_key_cooldown():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})
        await incident_diag._diag_task
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})

    run_mock.assert_awaited_once()  # second call within cooldown must not fire


@pytest.mark.asyncio
async def test_maybe_diagnose_noop_when_disabled():
    run_mock = AsyncMock(return_value="diagnosis text")
    with patch("backend.config.get_settings", return_value=_settings(incident_diag_enabled=False)), \
         patch("backend.agents.router.run_with_tools", run_mock):
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})

    assert incident_diag._diag_task is None
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnose_docker_button_omitted_for_unsafe_name():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose("docker:sneaky;rm -rf")

    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["buttons"] is None


@pytest.mark.asyncio
async def test_diagnose_docker_button_omitted_when_callback_too_long():
    run_mock = AsyncMock(return_value="diagnosis text")
    notify_mock = AsyncMock(return_value=True)
    long_name = "a" * 80  # docker:restart:<name> exceeds Telegram's 64-byte callback_data limit
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose(f"docker:{long_name}")

    assert notify_mock.await_args.kwargs["buttons"] is None


@pytest.mark.asyncio
async def test_diagnose_run_with_tools_exception_never_propagates():
    run_mock = AsyncMock(side_effect=RuntimeError("boom"))
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose("vm:100")  # must not raise

    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnose_budget_exceeded_skips_silently():
    from backend.safety.governor import BudgetExceeded
    run_mock = AsyncMock(side_effect=BudgetExceeded("daily", 4.0, 4.0, None))
    notify_mock = AsyncMock(return_value=True)
    with patch("backend.config.get_settings", return_value=_settings()), \
         patch("backend.agents.router.run_with_tools", run_mock), \
         patch("backend.events.notify_phone", notify_mock):
        await incident_diag.diagnose("vm:100")  # must not raise, must not notify

    notify_mock.assert_not_awaited()


# ---------------------------------------------------------------- G3a evidence block

@pytest.fixture
def graph_db(tmp_path, monkeypatch):
    from sqlalchemy import event
    from sqlmodel import SQLModel, create_engine

    import backend.database as bd

    e = create_engine(f"sqlite:///{tmp_path / 'g.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(e, "connect", bd._set_sqlite_pragmas)
    monkeypatch.setattr(bd, "engine", e)
    SQLModel.metadata.create_all(e)
    return e


def _seed_backup_incident():
    """Synthetic replay of the 2026-11-01 wrong diagnosis: the job STARTED at 21:00, the alert was raised
    later, and a guest was destroyed the next morning -- that destruction must not reach the prompt."""
    from datetime import datetime

    from sqlmodel import Session

    from backend.database import OutcomeFlag, engine
    from backend.graph import store
    from backend.graph.store import Edge, Node

    store.upsert_nodes([Node("host:pve", "host", "pve"), Node("guest:101", "guest", "lab-vm", source="proxmox_api"),
                        Node("guest:104", "guest", "old-box", source="proxmox_api"),
                        Node("guest:105", "guest", "no-backup", source="proxmox_api")], "2026-10-01T00:00:00")
    store.upsert_edges([Edge(f"guest:{g}", "host:pve", "runs_on", "proxmox_api") for g in (101, 104, 105)]
                       + [Edge(f"guest:{g}", "storage:BU", "backs_up_to", "proxmox_api") for g in (101, 104)],
                       "2026-10-01T00:00:00")
    store.insert_change("2026-10-31T13:00:00", "guest:105", "cores", "1", "2", "proxmox_api")  # not backed up: out of scope
    store.insert_change("2026-10-31T21:00:00", "guest:104", "memory", "2048", "4096", "proxmox_api")  # == job start
    store.insert_change("2026-10-31T21:30:00", "guest:101", "tags", "[]", '["lab"]', "proxmox_api")  # within lag
    store.insert_change("2026-10-31T12:00:00", "guest:104", "hookscript", None, "local:snippets/x.sh", "proxmox_api")
    store.insert_change("2026-10-29T12:00:00", "guest:104", "cores", "2", "4", "proxmox_api")  # > 24 h before: out
    store.insert_change("2026-11-01T07:44:00", "guest:101", "status", "active", "stale", "proxmox_api")  # after: out
    with Session(engine) as s:
        s.add(OutcomeFlag(source="homelab_watch", check="vzdump_failed", fingerprint="homelab_watch:vzdump_failed",
                          summary="backup failed", severity="high", detail='{"starttime": "2026-10-31T21:00:00"}',
                          created_at=datetime(2026, 11, 1, 2, 0)))
        s.commit()


async def test_evidence_scoped_strictly_before_job_start(graph_db):
    _seed_backup_incident()
    with patch("backend.config.get_settings", return_value=_settings(graph_enabled=True)), \
         patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=[])):
        ev = await incident_diag._evidence("vzdump_failed")
    assert ev.startswith("EVIDENCE (deterministic, from the knowledge graph)")
    assert "Incident at 2026-10-31T21:00:00Z UTC (job start)" in ev
    assert "guest:104 hookscript" in ev
    before, rest = ev.split("UNCERTAIN")
    assert "guest:104 memory" not in before and "guest:104 memory" in rest  # recorded AT the cutoff: not "before"
    assert "guest:101 tags" in rest  # recorded 30 min after the job start: listed, flagged uncertain
    assert "guest:101" in ev.split("Graph scope:")[1] and "guest:105" not in ev  # only backed-up guests in scope
    assert "guest:101 status" not in ev  # recorded ~11 h after the job start: never shown
    assert "cores" not in ev
    assert "recorded within 120 min AFTER" in ev  # two collect intervals
    assert "more than 120 min after the incident time are not listed" in ev


async def test_evidence_uses_flag_created_at_for_other_keys(graph_db):
    from datetime import datetime

    from sqlmodel import Session

    from backend.database import OutcomeFlag, engine

    _seed_backup_incident()
    with Session(engine) as s:
        s.add(OutcomeFlag(source="homelab_watch", check="vm:101", fingerprint="homelab_watch:vm:101", summary="stopped",
                          created_at=datetime(2026, 11, 1, 8, 0)))
        s.commit()
    with patch("backend.config.get_settings", return_value=_settings(graph_enabled=True)), \
         patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=[])):
        ev = await incident_diag._evidence("vm:101")
    assert "Incident at 2026-11-01T08:00:00Z UTC (alert raised)" in ev
    assert "guest:101 status: active -> stale" in ev and "guest:104" not in ev  # not a vzdump: ancestors only


async def test_evidence_in_prompt_only_when_graph_enabled(graph_db):
    _seed_backup_incident()
    for enabled, expect in ((False, False), (True, True)):
        run_mock = AsyncMock(return_value="diag")
        with patch("backend.config.get_settings", return_value=_settings(graph_enabled=enabled)), \
             patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=[])), \
             patch("backend.agents.router.run_with_tools", run_mock), \
             patch("backend.events.notify_phone", AsyncMock(return_value=True)):
            await incident_diag.diagnose("vzdump_failed")
        prompt = run_mock.await_args.args[2]
        assert ("EVIDENCE" in prompt) is expect
        assert prompt.endswith(incident_diag._user_prompt("vzdump_failed"))


async def test_evidence_never_raises(graph_db):
    with patch("backend.config.get_settings", return_value=_settings(graph_enabled=True)), \
         patch("backend.graph.store.ancestors", side_effect=RuntimeError("boom")):
        assert await incident_diag._evidence("vm:101") == ""
    with patch("backend.config.get_settings", return_value=_settings(graph_enabled=True)):
        assert await incident_diag._evidence("garage_open") == ""  # no graph node for it


async def test_evidence_without_reliable_incident_time(graph_db):
    from datetime import datetime

    from sqlmodel import Session, select

    from backend.database import OutcomeFlag, engine

    _seed_backup_incident()
    with Session(engine) as s:
        f = s.exec(select(OutcomeFlag)).one()
        f.detail = '{"starttime": "garbage"}'
        s.add(f)
        s.commit()
    patches = (patch("backend.config.get_settings", return_value=_settings(graph_enabled=True)),
               patch("backend.agents.outcomes.open_flags", new=AsyncMock(return_value=[])))
    with patches[0], patches[1]:
        ev = await incident_diag._evidence("vzdump_failed", datetime(2026, 11, 1, 9, 0))
        assert "Incident time unknown (no valid job start time)" in ev and "hookscript" not in ev
        # a vm alert whose flag is gone (closed / outcome flags off) falls back to the tick that fired it
        ev = await incident_diag._evidence("vm:101", datetime(2026, 11, 1, 8, 0))
        assert "Incident at 2026-11-01T08:00:00Z UTC (alert raised)" in ev and "guest:101 status" in ev
        ev = await incident_diag._evidence("vm:101")
        assert "Incident time unknown (alert time unknown)" in ev and "guest:101 status" not in ev


async def test_maybe_diagnose_passes_fire_time():
    seen = {}

    async def fake(key, fired_at=None):
        seen["at"] = fired_at

    with patch("backend.config.get_settings", return_value=_settings()), \
         patch.object(incident_diag, "diagnose", fake):
        incident_diag.maybe_diagnose({"vms": ["vm:100"], "docker": [], "array": [], "backups": []})
        await incident_diag._diag_task
    assert seen["at"] is not None and seen["at"].tzinfo is None


def test_collect_minutes_matches_scheduler_clamp():
    for configured, effective in ((5, 15), (60, 60)):
        with patch("backend.config.get_settings", return_value=_settings(graph_collect_minutes=configured)):
            assert incident_diag._collect_minutes() == effective
