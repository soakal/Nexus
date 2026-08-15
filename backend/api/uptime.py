from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import SpeedtestSample, UptimeSample, get_session

router = APIRouter()


@router.get("/summary")
async def get_uptime_summary(
    days: int = 7,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    samples = session.exec(
        select(UptimeSample)
        .where(UptimeSample.checked_at >= cutoff)
        .order_by(UptimeSample.checked_at)
    ).all()

    # Group by source. Historical rows from decommissioned integrations
    # (Hermes, retired 2026-08-09) are never deleted -- they're left to age
    # out of the query window naturally per this repo's own convention (see
    # CLAUDE.md's "Hermes fully decommissioned" entry) -- but nothing polls
    # them anymore, so they're excluded from the live summary instead of
    # waiting out the 7-day default window.
    _DECOMMISSIONED_SOURCES = {"hermes"}
    by_source: dict[str, list] = {}
    for s in samples:
        if s.source in _DECOMMISSIONED_SOURCES:
            continue
        by_source.setdefault(s.source, []).append(s)

    sources = []
    for source, entries in sorted(by_source.items()):
        total = len(entries)
        ok_count = sum(1 for e in entries if e.ok)
        uptime_pct = round(100 * ok_count / total, 1) if total > 0 else 0.0
        current_ok = entries[-1].ok if entries else False
        latencies = [e.latency_ms for e in entries if e.latency_ms is not None]
        avg_latency_ms = int(sum(latencies) / len(latencies)) if latencies else 0
        sources.append({
            "source": source,
            "uptime_pct": uptime_pct,
            "current_ok": current_ok,
            "avg_latency_ms": avg_latency_ms,
            "samples": total,
        })

    return {"sources": sources, "generated_at": datetime.utcnow().isoformat()}


@router.get("/speedtest")
async def get_speedtest(
    days: int = 7,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    samples = session.exec(
        select(SpeedtestSample)
        .where(SpeedtestSample.checked_at >= cutoff)
        .order_by(SpeedtestSample.checked_at)
    ).all()

    data = [
        {
            "timestamp": s.checked_at.isoformat(),
            "download_mbps": s.download_mbps,
            "upload_mbps": s.upload_mbps,
            "ping_ms": s.ping_ms,
        }
        for s in samples
    ]
    latest = data[-1] if data else None
    return {"data": data, "latest": latest}
