"""Tests for the Brain Organizer -> NEXUS spend ingestor (Tier C batch 1, C7).

The organizer subprocess appends token-usage JSON lines to usage.jsonl; the
ingestor atomically claims that file and writes one SpendLog row per line
(label="brain_organizer"), pricing known models via router._PRICE_PER_MTOK.
"""
import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Register all tables (incl. SpendLog) on metadata.
import backend.database  # noqa: F401


def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


@pytest.fixture
def usage_paths(monkeypatch, tmp_path):
    """Point the ingestor's usage/claim files at a temp dir."""
    import backend.agents.brain_spend as bs

    usage = tmp_path / "usage.jsonl"
    claim = tmp_path / "usage.jsonl.ingest"
    monkeypatch.setattr(bs, "_USAGE_FILE", usage)
    monkeypatch.setattr(bs, "_CLAIM_FILE", claim)
    return usage, claim


def _line(model, inp, out, ts="2026-07-01T12:00:00+00:00", provider="anthropic"):
    return json.dumps({
        "ts": ts, "model": model,
        "input_tokens": inp, "output_tokens": out,
        "provider": provider,
    })


def _rows(eng):
    from backend.database import SpendLog
    with Session(eng) as s:
        return s.exec(select(SpendLog)).all()


def test_happy_path_two_lines(eng, usage_paths):
    from backend.agents.brain_spend import ingest_brain_spend

    usage, _ = usage_paths
    usage.write_text(
        _line("claude-sonnet-4-6", 1_000_000, 1_000_000) + "\n"
        + _line("claude-haiku-4-5-20251001", 2_000_000, 0) + "\n",
        encoding="utf-8",
    )

    n = ingest_brain_spend()
    assert n == 2

    rows = sorted(_rows(eng), key=lambda r: r.model)
    assert len(rows) == 2
    assert all(r.label == "brain_organizer" for r in rows)
    # haiku: 2M input @ $1/MTok = $2.00, 0 output
    haiku = next(r for r in rows if r.model.startswith("claude-haiku"))
    assert haiku.cost_usd == pytest.approx(2.0)
    assert haiku.input_tokens == 2_000_000
    # sonnet: 1M in @ $3 + 1M out @ $15 = $18.00
    sonnet = next(r for r in rows if r.model.startswith("claude-sonnet"))
    assert sonnet.cost_usd == pytest.approx(18.0)


def test_unknown_model_zero_cost_but_records_tokens(eng, usage_paths):
    from backend.agents.brain_spend import ingest_brain_spend

    usage, _ = usage_paths
    usage.write_text(_line("some-unknown-model", 500, 500) + "\n", encoding="utf-8")

    assert ingest_brain_spend() == 1
    rows = _rows(eng)
    assert len(rows) == 1
    assert rows[0].cost_usd == 0.0
    assert rows[0].input_tokens == 500
    assert rows[0].output_tokens == 500


def test_openrouter_prefixed_model_prices(eng, usage_paths):
    """OpenRouter logs 'anthropic/claude-...'; the provider prefix is stripped so
    it prices against the same table instead of falling through to 0.0."""
    from backend.agents.brain_spend import ingest_brain_spend

    usage, _ = usage_paths
    usage.write_text(
        _line("anthropic/claude-sonnet-4-6", 1_000_000, 0, provider="openrouter") + "\n",
        encoding="utf-8",
    )
    assert ingest_brain_spend() == 1
    rows = _rows(eng)
    assert rows[0].cost_usd == pytest.approx(3.0)


def test_malformed_line_skipped(eng, usage_paths):
    from backend.agents.brain_spend import ingest_brain_spend

    usage, _ = usage_paths
    usage.write_text(
        "this is not json\n"
        + _line("claude-sonnet-4-6", 1_000_000, 0) + "\n",
        encoding="utf-8",
    )
    # 1 valid row, malformed line skipped (not raised).
    assert ingest_brain_spend() == 1
    assert len(_rows(eng)) == 1


def test_missing_file_no_op(eng, usage_paths):
    from backend.agents.brain_spend import ingest_brain_spend

    # Neither usage.jsonl nor the claim file exists.
    assert ingest_brain_spend() == 0
    assert _rows(eng) == []


def test_leftover_claim_consumed_first(eng, usage_paths):
    """A leftover .ingest file (previous cycle crashed mid-commit) is ingested on
    the next run BEFORE (and in addition to) any fresh usage.jsonl."""
    from backend.agents.brain_spend import ingest_brain_spend

    usage, claim = usage_paths
    claim.write_text(_line("claude-haiku-4-5-20251001", 1_000_000, 0) + "\n", encoding="utf-8")
    usage.write_text(_line("claude-sonnet-4-6", 1_000_000, 0) + "\n", encoding="utf-8")

    n = ingest_brain_spend()
    assert n == 2  # leftover claim + fresh usage both ingested
    models = {r.model for r in _rows(eng)}
    assert models == {"claude-haiku-4-5-20251001", "claude-sonnet-4-6"}
    # Both files consumed.
    assert not usage.exists()
    assert not claim.exists()


def test_today_spend_includes_ingested_row(eng, usage_paths, monkeypatch):
    """An ingested row (created_at = now) is summed by governor.today_spend_usd()."""
    from datetime import datetime
    from backend.agents.brain_spend import ingest_brain_spend
    from backend.safety import governor

    usage, _ = usage_paths
    # Use a current-day ts so it lands inside today's window.
    now_iso = datetime.utcnow().isoformat()
    usage.write_text(_line("claude-sonnet-4-6", 1_000_000, 0, ts=now_iso) + "\n", encoding="utf-8")

    before = governor.today_spend_usd()
    assert ingest_brain_spend() == 1
    after = governor.today_spend_usd()
    assert after - before == pytest.approx(3.0)
