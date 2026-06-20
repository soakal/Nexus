import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import SegmentedControl from '../components/SegmentedControl'
import ScreenHeader from '../components/ScreenHeader'
import AreaChart from '../components/AreaChart'

function getHiLo(data) {
  const vals = (data || []).map(d => (typeof d === 'number' ? d : d?.value)).filter(v => v != null)
  if (!vals.length) return { hi: null, lo: null }
  return { hi: Math.max(...vals), lo: Math.min(...vals) }
}

function fmt(val, unit) {
  if (val == null) return '—'
  return unit === '%' ? `${val.toFixed(1)}%` : `${val.toFixed(1)} GB`
}

export default function Trends() {
  const [days, setDays] = useState(7)
  const [unraidTrend, setUnraidTrend] = useState(null)
  const [channelsTrend, setChannelsTrend] = useState(null)
  const [adguardTrend, setAdguardTrend] = useState(null)

  useEffect(() => {
    // Reset to null on range change to avoid stale chart flash
    setUnraidTrend(null)
    setChannelsTrend(null)
    setAdguardTrend(null)

    api.trends.get('unraid', 'storage_used_gb', days).then(setUnraidTrend).catch(() => {})
    api.trends.get('channels', 'storage_used_gb', days).then(setChannelsTrend).catch(() => {})
    api.trends.get('adguard', 'blocked_pct', days).then(setAdguardTrend).catch(() => {})
  }, [days])

  const charts = [
    { label: 'Unraid Storage', data: unraidTrend?.data || [], color: '#2fd4ee', unit: 'GB' },
    { label: 'Channels DVR Storage', data: channelsTrend?.data || [], color: '#5b8cff', unit: 'GB' },
    { label: 'AdGuard Blocked %', data: adguardTrend?.data || [], color: '#34d399', unit: '%' },
  ]

  return (
    <div style={{
      width: '100%',
      maxWidth: '1100px',
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>
      <ScreenHeader
        section="Trends"
        title="Intelligence Feed"
        right={
          <SegmentedControl
            options={[
              { value: 7, label: '7D' },
              { value: 30, label: '30D' },
              { value: 90, label: '90D' },
            ]}
            value={days}
            onChange={setDays}
          />
        }
      />

      {!unraidTrend && (
        <div style={{ color: '#5d6982', fontSize: '13px' }}>Loading trends…</div>
      )}

      {charts.map(({ label, data, color, unit }) => {
        const { hi, lo } = getHiLo(data)
        return (
          <Card key={label}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
              <Eyebrow>{label}</Eyebrow>
              <span style={{ fontSize: '11px', color: '#5d6982' }}>
                Hi {fmt(hi, unit)}
              </span>
            </div>
            <AreaChart data={data} color={color} height={150} />
            <div style={{ fontSize: '11px', color: '#5d6982', marginTop: '4px' }}>
              Lo {fmt(lo, unit)}
            </div>
          </Card>
        )
      })}
    </div>
  )
}
