import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import RecordingCard from '../components/RecordingCard'
export default function Media() {
  const [data, setData] = useState(null)
  useEffect(() => { api.channels.get().then(setData).catch(() => {}) }, [])
  return (
    <div className="p-6 max-w-2xl">
      <h1 className="font-mono text-accent-cyan text-xl font-bold mb-6">CHANNELS DVR</h1>
      {!data ? <div className="text-text-secondary">Loading...</div> : (
        <div className="space-y-6">
          <div>
            <h2 className="text-text-secondary text-xs uppercase tracking-wider mb-2">Now Recording</h2>
            {data.recording_now?.length > 0
              ? data.recording_now.map((r, i) => <RecordingCard key={i} recording={r} />)
              : <div className="text-text-secondary text-sm">Nothing recording</div>}
          </div>
          <div>
            <h2 className="text-text-secondary text-xs uppercase tracking-wider mb-2">Upcoming</h2>
            <div className="space-y-2">
              {(data.upcoming || []).map((r, i) => (
                <div key={i} className="bg-bg-card border border-border-dark rounded p-3 text-sm">
                  <span className="text-text-primary">{r.title}</span>
                  <span className="text-text-secondary ml-2">{r.channel}</span>
                </div>
              ))}
            </div>
          </div>
          <div>
            <h2 className="text-text-secondary text-xs uppercase tracking-wider mb-2">Storage</h2>
            <div className="w-full bg-border-dark rounded-full h-3">
              <div className="bg-accent-cyan h-3 rounded-full" style={{ width: `${data.storage_total_gb ? (data.storage_used_gb / data.storage_total_gb * 100) : 0}%` }} />
            </div>
            <div className="text-text-secondary text-xs mt-1">{data.storage_used_gb}GB used of {data.storage_total_gb}GB</div>
          </div>
        </div>
      )}
    </div>
  )
}
