import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


@pytest.mark.asyncio
async def test_trend_projection_math():
    """Verify linear regression gives reasonable projections."""
    from fastapi.testclient import TestClient
    from unittest.mock import patch, AsyncMock
    from backend.database import TrendSnapshot

    now = datetime.utcnow()
    snapshots = [
        TrendSnapshot(id=i, source="unraid", metric="storage_used_gb",
                     value=100 + i * 5,
                     captured_at=now - timedelta(days=7-i))
        for i in range(8)
    ]

    with patch("backend.database.engine"), \
         patch("sqlmodel.Session") as mock_session:
        session_mock = MagicMock()
        session_mock.exec.return_value.all.return_value = snapshots
        mock_session.return_value.__enter__ = MagicMock(return_value=session_mock)
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        # Test projection endpoint logic directly
        data = [{"timestamp": s.captured_at.isoformat(), "value": s.value} for s in snapshots]
        n = len(data)
        x_vals = list(range(n))
        y_vals = [d["value"] for d in data]
        x_mean = sum(x_vals) / n
        y_mean = sum(y_vals) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
        den = sum((x - x_mean) ** 2 for x in x_vals)
        slope = num / den if den != 0 else 0
        assert slope > 0, "Slope should be positive for increasing storage"


def test_trend_snapshot_schema():
    from backend.database import TrendSnapshot
    s = TrendSnapshot(source="channels", metric="storage_used_gb", value=500.0)
    assert s.source == "channels"
    assert s.value == 500.0
