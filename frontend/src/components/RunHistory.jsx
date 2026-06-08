export default function RunHistory({ runs }) {
  if (!runs?.length) return <div className="text-text-secondary text-sm">No runs yet.</div>
  return (
    <div className="space-y-2">
      {runs.map(r => {
        const ok = r.success
        return (
          <div
            key={r.id}
            className={`rounded-lg p-3 border ${
              ok
                ? 'bg-bg-card border-border-dark'
                : 'bg-accent-orange/10 border-accent-orange/60'
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-text-primary text-sm truncate flex-1">{r.prompt_snippet}</span>
              <span
                className={`text-xs font-mono ml-2 px-1.5 py-0.5 rounded ${
                  ok
                    ? 'text-accent-green bg-accent-green/10'
                    : 'text-accent-orange bg-accent-orange/20 font-bold'
                }`}
              >
                {ok ? 'ok' : 'FAIL'}
              </span>
            </div>
            {!ok && r.output_snippet && (
              <div className="text-accent-orange text-xs mt-1 truncate">{r.output_snippet}</div>
            )}
            <div className="flex gap-4 mt-1 text-text-secondary text-xs">
              <span className="font-mono">{r.model}</span>
              <span>{r.duration_ms}ms</span>
              <span>{new Date(r.created_at.endsWith('Z') ? r.created_at : r.created_at + 'Z').toLocaleString()}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
