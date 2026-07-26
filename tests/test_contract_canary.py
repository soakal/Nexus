"""Tests for backend/safety/contracts.py (Integration Contract Canary — Feature 1)."""
from dataclasses import dataclass, field

from backend.safety import contracts


@dataclass
class _FakeUnraid:
    array_status: str = "started"
    storage_total_gb: float = 4000.0
    cpu_pct: float = 0.0


@dataclass
class _FakeHA:
    entities: list = field(default_factory=lambda: ["sensor.a"])
    alerts: list = field(default_factory=list)


def test_check_object_passes_healthy():
    c = (
        contracts.FieldContract("array_status", (str,), "not_default", default="unknown", consumer="x:1"),
        contracts.FieldContract("storage_total_gb", (int, float), "positive", consumer="x:2"),
    )
    assert contracts.check_object(_FakeUnraid(), c) == []


def test_check_object_flags_sentinel_default():
    c = (contracts.FieldContract("array_status", (str,), "not_default", default="unknown", consumer="briefing.py:232"),)
    breaches = contracts.check_object(_FakeUnraid(array_status="unknown"), c)
    assert len(breaches) == 1
    assert "array_status" in breaches[0]
    assert "briefing.py:232" in breaches[0]


def test_check_object_flags_zero_total():
    c = (contracts.FieldContract("storage_total_gb", (int, float), "positive", consumer="x:1"),)
    assert contracts.check_object(_FakeUnraid(storage_total_gb=0.0), c) != []
    assert contracts.check_object(_FakeUnraid(storage_total_gb=1.0), c) == []


def test_check_object_missing_field_is_breach_not_raise():
    c = (contracts.FieldContract("does_not_exist", (str,), "type", consumer="x:1"),)
    breaches = contracts.check_object(_FakeUnraid(), c)
    assert len(breaches) == 1
    assert "missing" in breaches[0]


def test_is_default_rule_inverted():
    c = (contracts.FieldContract("cpu_pct", (int, float), "is_default", default=0.0, consumer="tools.py:142"),)
    assert contracts.check_object(_FakeUnraid(cpu_pct=0.0), c) == []
    assert contracts.check_object(_FakeUnraid(cpu_pct=42.0), c) != []


def test_empty_collection_allowed_where_legitimate():
    c_alerts = (contracts.FieldContract("alerts", (list,), "type", consumer="briefing.py:224"),)
    c_entities = (contracts.FieldContract("entities", (list,), "nonempty", consumer="briefing.py:223"),)
    assert contracts.check_object(_FakeHA(alerts=[]), c_alerts) == []
    assert contracts.check_object(_FakeHA(entities=[]), c_entities) != []


def test_every_integration_is_covered():
    # Imports the REAL registry (not a hardcoded copy) so this test actually
    # fails if a 13th integration is added to sources.py without a matching
    # contracts.py entry — a copied literal would drift silently instead.
    from backend.api.sources import REGISTRY_NAMES

    missing = set(REGISTRY_NAMES) - contracts.covered()
    assert not missing, (
        f"Integration(s) {missing} are in backend/api/sources.py's REGISTRY_NAMES but have "
        f"no backend/safety/contracts.py CONTRACTS or EXCLUDED entry — add one."
    )


def test_every_contract_names_a_consumer():
    for name, field_contracts in contracts.CONTRACTS.items():
        for c in field_contracts:
            assert c.consumer, f"{name}.{c.field} has a blank consumer"
            assert ":" in c.consumer, f"{name}.{c.field}'s consumer {c.consumer!r} should be a file:line"
