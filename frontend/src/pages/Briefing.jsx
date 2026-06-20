import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import BriefingPanel from '../components/BriefingPanel'
import { fmtDateTime } from '../lib/parseUTC'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'

export default function Briefing() {
  const [briefing, setBriefing] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.briefing.latest().then(b => setBriefing(b)).catch(() => {})
  }, [])

  const trigger = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await api.briefing.trigger()
      setBriefing({ content: r.briefing, created_at: new Date().toISOString() })
    } catch (e) {
      setError('Failed to generate briefing.')
    }
    setLoading(false)
  }

  const subline = briefing
    ? 'Generated ' + new Date(briefing.created_at || Date.now()).toLocaleString()
    : null

  return (
    <div style={{
      width: '100%',
      maxWidth: 1000,
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>
      <ScreenHeader
        section="Briefing"
        title="Intel Briefing"
        subline={subline}
        right={
          <PrimaryButton
            onClick={trigger}
            disabled={loading}
            icon={
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 5v14M5 12h14"/>
              </svg>
            }
          >
            {loading ? 'Generating…' : 'Generate'}
          </PrimaryButton>
        }
      />

      {loading && !briefing && (
        <div style={{ color: '#5d6982', fontSize: 13 }}>Generating briefing…</div>
      )}

      {error && (
        <div style={{ color: '#fb7185', fontSize: 13 }}>{error}</div>
      )}

      {briefing && <BriefingPanel content={briefing.content} />}
    </div>
  )
}
