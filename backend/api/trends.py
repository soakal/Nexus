from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import TrendSnapshot, get_session

router = APIRouter()


@router.get("/{source}/{metric}")
async def get_trend(
    source: str,
    metric: str,
    days: int = 7,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    snapshots = session.exec(
        select(TrendSnapshot)
        .where(TrendSnapshot.source == source)
        .where(TrendSnapshot.metric == metric)
        .where(TrendSnapshot.captured_at >= cutoff)
        .order_by(TrendSnapshot.captured_at)
    ).all()

    if not snapshots:
        return {"source": source, "metric": metric, "data": [], "projection": None}

    data = [{"timestamp": s.captured_at.isoformat(), "value": s.value} for s in snapshots]

    # Linear regression projection
    projection = None
    if len(data) >= 2:
        n = len(data)
        x_vals = list(range(n))
        y_vals = [d["value"] for d in data]
        x_mean = sum(x_vals) / n
        y_mean = sum(y_vals) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals, strict=False))
        den = sum((x - x_mean) ** 2 for x in x_vals)
        slope = num / den if den != 0 else 0
        intercept = y_mean - slope * x_mean

        # Project 14 days forward
        future_points = []
        for i in range(1, 15):
            future_x = n + i * (n / days) if days > 0 else n + i
            projected_val = slope * future_x + intercept
            future_ts = (datetime.utcnow() + timedelta(days=i)).isoformat()
            future_points.append({"timestamp": future_ts, "value": round(projected_val, 2)})
        projection = future_points

    return {"source": source, "metric": metric, "data": data, "projection": projection}
