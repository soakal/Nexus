import { useState } from 'react'
import { Eye, EyeOff, CheckCircle, XCircle } from 'lucide-react'
import { api } from '../lib/api'
export default function SecretField({ secretKey, label, lastSet, missing = false }) {
  const [visible, setVisible] = useState(false)
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [testResult, setTestResult] = useState(null)
  const [loading, setLoading] = useState(false)

  const test = async () => {
    setLoading(true)
    try {
      const r = await api.secrets.test(secretKey)
      setTestResult(r)
    } catch { setTestResult({ ok: false, error: 'Request failed' }) }
    setLoading(false)
  }
  const save = async () => {
    if (!value) return
    await api.secrets.set(secretKey, value)
    setEditing(false)
    setValue('')
  }

  return (
    <div className="flex items-center gap-3 py-2 border-b border-border-dark last:border-0">
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <span className="text-text-primary text-sm">{label}</span>
          {missing && (
            <span className="text-xs font-mono px-1.5 py-0.5 rounded" style={{ background: 'rgba(239,68,68,0.15)', color: '#ef4444', border: '1px solid rgba(239,68,68,0.4)' }}>
              MISSING — required
            </span>
          )}
        </div>
        {lastSet && <div className="text-text-secondary text-xs font-mono">Last set: {new Date(lastSet.endsWith('Z') ? lastSet : lastSet + 'Z').toLocaleDateString()}</div>}
      </div>
      {editing ? (
        <div className="flex gap-2 items-center">
          <div className="relative">
            <input
              type={visible ? 'text' : 'password'}
              value={value}
              onChange={e => setValue(e.target.value)}
              className="hud-input w-48 pr-8"
              placeholder="New value..."
            />
            <button
              type="button"
              onClick={() => setVisible(v => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-text-secondary hover:text-accent-cyan"
              title={visible ? 'Hide' : 'Show'}
            >
              {visible ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          <button onClick={save} className="glow-btn text-xs px-2 py-1">Save</button>
          <button onClick={() => setEditing(false)} className="hud-label hover:text-text-primary">Cancel</button>
        </div>
      ) : (
        <div className="flex gap-2 items-center">
          <span className="font-mono text-text-secondary text-sm tracking-widest">••••••••</span>
          <button onClick={() => setEditing(true)} className="text-accent-cyan text-xs glow-on-hover">Edit</button>
          <button onClick={test} disabled={loading} className="text-text-secondary text-xs hover:text-text-primary">
            {loading ? '...' : 'Test'}
          </button>
          {testResult && (
            testResult.ok
              ? <CheckCircle size={14} className="text-accent-green" />
              : <span title={testResult.error}><XCircle size={14} className="text-accent-red" /></span>
          )}
        </div>
      )}
    </div>
  )
}
