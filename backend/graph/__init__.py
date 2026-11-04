"""NEXUS knowledge graph (plan-v3). Storage in store.py; everything else builds on it."""
import re
from datetime import UTC, datetime, timedelta

INFRA_TYPES = frozenset({"host", "guest", "container", "service", "endpoint", "storage", "external"})


def enabled() -> bool:
    from backend.config import get_settings

    return bool(getattr(get_settings(), "graph_enabled", False))


def now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat()


_REL = re.compile(r"^\s*(\d+)\s*([mhd])\s*$")
_REL_UNIT = {"m": "minutes", "h": "hours", "d": "days"}


def to_utc_iso(value: str | datetime) -> str:
    """'24h'/'30m'/'7d', ISO 8601 with or without offset (naive = UTC), or a datetime
    -> the naive-UTC seconds form now_iso() writes. ValueError on anything else."""
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            m = _REL.match(str(value))
            if m:
                dt = datetime.now(UTC) - timedelta(**{_REL_UNIT[m.group(2)]: int(m.group(1))})
            else:
                dt = datetime.fromisoformat(str(value).strip())  # raises ValueError; accepts 'Z' and offsets
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC)
    except OverflowError as e:  # '9999999999h', '0001-01-01T00:00:00+01:00': bad input, not a server error
        raise ValueError(f"time out of range: {value!r}") from e
    return dt.replace(tzinfo=None, microsecond=0).isoformat()


def intervals(settings=None) -> dict[str, int]:
    """Effective job intervals in minutes -- the scheduler, freshness checks and incident evidence share these clamps."""
    if settings is None:
        from backend.config import get_settings

        settings = get_settings()
    return {
        "collect": max(15, int(getattr(settings, "graph_collect_minutes", 60))),
        "vault_index": max(5, int(getattr(settings, "graph_vault_index_minutes", 15))),
    }
