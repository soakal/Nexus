import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import TrendChart from '../components/TrendChart'

const RANGE_OPTIONS = [
  { label: '7D',  days: 7 },
  { label: '30D', days: 30 },
  { label: '90D', days: 90 },
]

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

  return (
    <div className="p-4 md:p-6 max-w-3xl space-y-6">
      <h1 className="page-header">INTELLIGENCE FEED</h1>

      {/* Range selector */}
      <div className="flex items-center gap-2">
        {RANGE_OPTIONS.map(({ label, days: d }) => {
          const active = d === days
          return (
            <button
              key={d}
              onClick={() => setDays(d)}
              className="glow-btn"
              style={{
                opacity: active ? 1 : 0.4,
                boxShadow: active
                  ? '0 0 10px rgba(0,212,255,0.5)'
                  : 'none',
                fontWeight: active ? '600' : '400',
                transition: 'all 0.15s ease',
              }}
            >
              {label}
            </button>
          )
        })}
      </div>

      <TrendChart title="Unraid Storage Used (GB)" data={unraidTrend?.data} projection={unraidTrend?.projection} unit="GB" />
      <TrendChart title="Channels DVR Storage Used (GB)" data={channelsTrend?.data} projection={channelsTrend?.projection} unit="GB" />
      <TrendChart title="AdGuard Blocked %" data={adguardTrend?.data} projection={adguardTrend?.projection} unit="%" />
    </div>
  )
}
