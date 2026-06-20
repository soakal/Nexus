export default function RunHistory({ runs }) {
  if (!runs?.length) return (
    <div style={{ color: '#5d6982', fontSize: 13, fontStyle: 'italic' }}>
      No runs yet.
    </div>
  )

  return (
    <>
      {runs.map(r => {
        const ok = r.success
        return (
          <div
            key={r.id}
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 14,
              background: 'linear-gradient(180deg,rgba(255,255,255,0.022),rgba(255,255,255,0)),#0c1320',
              border: '1px solid rgba(120,160,220,0.10)',
              borderRadius: 13,
              padding: '14px 16px',
            }}
          >
            {/* Left column */}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, color: '#cdd6e6', lineHeight: 1.55 }}>
                {r.prompt_snippet || r.prompt || r.description || '—'}
              </div>

              {/* Meta row */}
              <div style={{
                marginTop: 7,
                fontSize: 11,
                color: '#5d6982',
                fontFamily: "'JetBrains Mono', monospace",
                display: 'flex',
                gap: 10,
                flexWrap: 'wrap',
                alignItems: 'center',
              }}>
                <span style={{ color: 'var(--accent)' }}>{r.model}</span>
                {r.duration_ms != null && <span>{r.duration_ms}ms</span>}
                <span>
                  {new Date(
                    r.created_at
                      ? (r.created_at.endsWith('Z') ? r.created_at : r.created_at + 'Z')
                      : Date.now()
                  ).toLocaleString()}
                </span>
              </div>

              {/* Failure output snippet */}
              {!ok && r.output_snippet && (
                <div style={{
                  marginTop: 6,
                  fontSize: 11,
                  color: '#fb7185',
                  fontFamily: "'JetBrains Mono', monospace",
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {r.output_snippet}
                </div>
              )}
            </div>

            {/* Right badge */}
            <div style={{
              flexShrink: 0,
              color: ok ? '#5fe0b4' : '#fb7185',
              background: ok ? 'rgba(52,211,153,0.1)' : 'rgba(251,113,133,0.1)',
              border: ok
                ? '1px solid rgba(52,211,153,0.25)'
                : '1px solid rgba(251,113,133,0.25)',
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: '0.06em',
              padding: '4px 9px',
              borderRadius: 6,
              alignSelf: 'flex-start',
            }}>
              {ok ? 'OK' : 'FAIL'}
            </div>
          </div>
        )
      })}
    </>
  )
}
