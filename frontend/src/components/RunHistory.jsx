export default function RunHistory({ runs }) {
  if (!runs?.length) return <div className="text-text-secondary text-sm italic">No runs yet.</div>
  return (
    <div className="space-y-1.5">
      {runs.map(r => {
        const ok = r.success
        return (
          <div
            key={r.id}
            className="hud-panel-sm p-3 flex flex-col gap-1"
            style={{ borderColor: ok ? 'rgba(0,255,157,0.15)' : 'rgba(255,45,45,0.3)' }}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-text-primary text-sm truncate flex-1">{r.prompt_snippet}</span>
              <span
                className={`hud-label px-2 py-0.5 border ${
                  ok
                    ? 'text-accent-green bg-accent-green/10 border-accent-green/20'
                    : 'text-accent-red bg-accent-red/10 border-accent-red/30'
                }`}
              >
                {ok ? 'ok' : 'FAIL'}
              </span>
            </div>
            {!ok && r.output_snippet && (
              <div className="text-accent-red text-xs truncate">{r.output_snippet}</div>
            )}
            <div className="flex gap-4 items-center">
              <span className="font-mono text-xs bg-accent-blue/20 text-text-secondary px-2 py-0.5">{r.model}</span>
              <span className="font-mono text-xs text-text-secondary">{r.duration_ms}ms</span>
              <span className="font-mono text-xs text-text-secondary">{new Date(r.created_at.endsWith('Z') ? r.created_at : r.created_at + 'Z').toLocaleString()}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
