import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import TrendChart from '../components/TrendChart'
export default function Trends() {
  const [unraidTrend, setUnraidTrend] = useState(null)
  const [channelsTrend, setChannelsTrend] = useState(null)
  const [adguardTrend, setAdguardTrend] = useState(null)
  useEffect(() => {
    api.trends.get('unraid', 'storage_used_gb', 7).then(setUnraidTrend).catch(() => {})
    api.trends.get('channels', 'storage_used_gb', 7).then(setChannelsTrend).catch(() => {})
    api.trends.get('adguard', 'blocked_pct', 7).then(setAdguardTrend).catch(() => {})
  }, [])
  return (
    <div className="p-6 max-w-3xl space-y-6">
      <h1 className="font-mono text-accent-cyan text-xl font-bold">TREND TRACKING</h1>
      <TrendChart title="Unraid Storage Used (GB)" data={unraidTrend?.data} projection={unraidTrend?.projection} unit="GB" />
      <TrendChart title="Channels DVR Storage Used (GB)" data={channelsTrend?.data} projection={channelsTrend?.projection} unit="GB" />
      <TrendChart title="AdGuard Blocked %" data={adguardTrend?.data} projection={adguardTrend?.projection} unit="%" />
    </div>
  )
}
