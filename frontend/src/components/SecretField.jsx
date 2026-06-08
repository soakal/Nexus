import { useState } from 'react'
import { Eye, EyeOff, CheckCircle, XCircle } from 'lucide-react'
import { api } from '../lib/api'
export default function SecretField({ secretKey, label, lastSet }) {
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
        <div className="text-text-primary text-sm">{label}</div>
        {lastSet && <div className="text-text-secondary text-xs">Last set: {new Date(lastSet.endsWith('Z') ? lastSet : lastSet + 'Z').toLocaleDateString()}</div>}
      </div>
      {editing ? (
        <div className="flex gap-2 items-center">
          <input type="password" value={value} onChange={e => setValue(e.target.value)}
            className="bg-bg-secondary border border-border-dark rounded px-2 py-1 text-sm text-text-primary w-48 font-mono"
            placeholder="New value..." />
          <button onClick={save} className="text-accent-green text-xs">Save</button>
          <button onClick={() => setEditing(false)} className="text-text-secondary text-xs">Cancel</button>
        </div>
      ) : (
        <div className="flex gap-2 items-center">
          <span className="font-mono text-text-secondary text-sm">••••••••</span>
          <button onClick={() => setEditing(true)} className="text-accent-cyan text-xs hover:underline">Edit</button>
          <button onClick={test} disabled={loading} className="text-text-secondary text-xs hover:text-text-primary">
            {loading ? '...' : 'Test'}
          </button>
          {testResult && (
            testResult.ok
              ? <CheckCircle size={14} className="text-accent-green" />
              : <XCircle size={14} className="text-accent-orange" title={testResult.error} />
          )}
        </div>
      )}
    </div>
  )
}

