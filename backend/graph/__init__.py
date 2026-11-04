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
    return dt.replace(tzinfo=None, microsecond=0).isoformat()
