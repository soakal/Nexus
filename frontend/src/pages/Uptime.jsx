import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import AreaChart from '../components/AreaChart'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusDot from '../components/StatusDot'
import ScreenHeader from '../components/ScreenHeader'

export default function Uptime() {
  const [summary, setSummary] = useState(null)
  const [speedtest, setSpeedtest] = useState(null)
  const [liveStatus, setLiveStatus] = useState({})

  const load = useCallback(() => {
    api.uptime.summary(7).then(setSummary).catch(() => {})
    api.uptime.speedtest(7).then(setSpeedtest).catch(() => {})
    // Live current status comes from the same cached health checks the dashboard
    // uses, so the dot recovers in seconds instead of lagging the 2-min sampler.
    api.sources.status().then(setLiveStatus).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 30000)
    const onVis = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [load])

  // Map speedtest history for AreaChart
  const downloadChartData = speedtest?.data?.map(h => ({ value: h.download_mbps })) || []

  return (
    <div style={{
      width: '100%',
      maxWidth: 1200,
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Uptime" title="Uptime & Connectivity" />

      {/* Section 1 — Source Uptime */}
      {!summary ? (
        <div style={{ color: '#5d6982', fontSize: '13px' }}>Loading…</div>
      ) : (
        <div>
          <Eyebrow style={{ marginBottom: 12 }}>Source Uptime — Last 7 Days</Eyebrow>
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill,minmax(220px,1fr))',
            gap: 12,
          }}>
            {(summary?.sources || []).map(s => {
              const isUp = liveStatus[s.source]?.healthy ?? s.current_ok
              return (
                <Card key={s.source} style={{ borderRadius: 14, padding: 16 }}>
                  {/* Top row: name + status */}
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                    <span style={{ fontSize: 13, fontWeight: 600, color: '#dbe3f0' }}>
                      {s.source}
                    </span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                      <StatusDot status={isUp ? 'green' : 'red'} />
                      <span style={{ fontSize: 11, fontWeight: 600, color: isUp ? '#5fe0b4' : '#fb7185' }}>
                        {isUp ? 'UP' : 'DOWN'}
                      </span>
                    </div>
                  </div>
                  {/* Big pct */}
                  <div style={{
                    fontSize: 26,
                    fontWeight: 700,
                    color: isUp ? 'var(--accent)' : '#fbbf24',
                    lineHeight: 1.1,
                  }}>
                    {s.uptime_pct}%
                  </div>
                  {/* Sub */}
                  <div style={{ fontSize: 11, color: '#5d6982', marginTop: 4 }}>
                    {s.avg_latency_ms}ms avg · {s.samples} samples
                  </div>
                </Card>
              )
            })}
          </div>
        </div>
      )}

      {/* Section 2 — Internet Speed */}
      <div>
        <Eyebrow style={{ marginBottom: 12 }}>Internet Speed</Eyebrow>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
          <Card style={{ flex: '1 1 160px', padding: 16, borderRadius: 14 }}>
            <Eyebrow style={{ marginBottom: 6 }}>Download</Eyebrow>
            <div style={{ fontSize: 26, fontWeight: 700, color: 'var(--accent)', lineHeight: 1.1 }}>
              {speedtest?.latest?.download_mbps || '—'}
            </div>
            <div style={{ fontSize: 13, color: '#5d6982', fontWeight: 500, marginTop: 2 }}>Mbps</div>
          </Card>
          <Card style={{ flex: '1 1 160px', padding: 16, borderRadius: 14 }}>
            <Eyebrow style={{ marginBottom: 6 }}>Upload</Eyebrow>
            <div style={{ fontSize: 26, fontWeight: 700, color: 'var(--accent)', lineHeight: 1.1 }}>
              {speedtest?.latest?.upload_mbps || '—'}
            </div>
            <div style={{ fontSize: 13, color: '#5d6982', fontWeight: 500, marginTop: 2 }}>Mbps</div>
          </Card>
          <Card style={{ flex: '1 1 160px', padding: 16, borderRadius: 14 }}>
            <Eyebrow style={{ marginBottom: 6 }}>Ping</Eyebrow>
            <div style={{ fontSize: 26, fontWeight: 700, color: '#fbbf24', lineHeight: 1.1 }}>
              {speedtest?.latest?.ping_ms || '—'}
            </div>
            <div style={{ fontSize: 13, color: '#5d6982', fontWeight: 500, marginTop: 2 }}>ms</div>
          </Card>
        </div>
      </div>

      {/* Section 3 — Download Speed chart */}
      <Card style={{ borderRadius: 14, padding: 16 }}>
        <Eyebrow style={{ marginBottom: 12 }}>Download Speed (Mbps)</Eyebrow>
        <AreaChart
          data={downloadChartData}
          color="#2fd4ee"
          height={160}
          gridLines={[45, 95]}
        />
      </Card>
    </div>
  )
}
