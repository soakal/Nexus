"""NEXUS knowledge graph (plan-v3). Storage in store.py; everything else builds on it."""
from datetime import UTC, datetime

INFRA_TYPES = frozenset({"host", "guest", "container", "service", "endpoint", "storage", "external"})


def enabled() -> bool:
    from backend.config import get_settings

    return bool(getattr(get_settings(), "graph_enabled", False))


def now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat()
