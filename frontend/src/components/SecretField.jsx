import { useState } from 'react'
import { Eye, EyeOff, CheckCircle, XCircle } from 'lucide-react'
import { api } from '../lib/api'
import TextInput from './TextInput'
import PrimaryButton from './PrimaryButton'
import GhostButton from './GhostButton'

export default function SecretField({ secretKey, label, lastSet, missing = false, onDelete, onSave }) {
  const [visible, setVisible] = useState(false)
  const [editing, setEditing] = useState(false)
  const [inputVal, setInputVal] = useState('')
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
    if (!inputVal) return
    await api.secrets.set(secretKey, inputVal)
    setEditing(false)
    setInputVal('')
    onSave?.()
  }

  const cancel = () => {
    setEditing(false)
    setInputVal('')
  }

  const lastSetDisplay = lastSet
    ? `Last set: ${new Date(lastSet.endsWith('Z') ? lastSet : lastSet + 'Z').toLocaleDateString()}`
    : null

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: '12px',
      flexWrap: 'wrap',
      padding: '13px 0',
      borderBottom: '1px solid rgba(120,160,220,0.07)',
    }}>
      {/* Left: label + last set */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: '14px', fontWeight: 600, color: '#dbe3f0' }}>{label}</div>
        {lastSetDisplay
          ? <div style={{ fontSize: '11px', color: '#5d6982', marginTop: '2px' }}>{lastSetDisplay}</div>
          : <div style={{ fontSize: '11px', color: '#f4d27a', marginTop: '2px' }}>Not set</div>
        }
      </div>

      {/* Right: view or edit cluster */}
      {editing ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <div style={{ position: 'relative' }}>
            <TextInput
              type={visible ? 'text' : 'password'}
              value={inputVal}
              onChange={e => setInputVal(e.target.value)}
              placeholder="New value..."
              style={{ width: '200px', paddingRight: '36px', fontSize: '13px', fontFamily: "'JetBrains Mono', monospace" }}
            />
            <button
              type="button"
              onClick={() => setVisible(v => !v)}
              style={{
                position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)',
                background: 'none', border: 'none', cursor: 'pointer', color: '#8a96ad',
                display: 'flex', alignItems: 'center',
              }}
              title={visible ? 'Hide' : 'Show'}
            >
              {visible ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          <PrimaryButton onClick={save} disabled={!inputVal} style={{ padding: '7px 14px', fontSize: '12px' }}>Save</PrimaryButton>
          <GhostButton onClick={cancel} style={{ padding: '7px 12px', fontSize: '12px' }}>Cancel</GhostButton>
          {testResult && (
            testResult.ok
              ? <CheckCircle size={14} color="#5fe0b4" />
              : <span title={testResult.error}><XCircle size={14} color="#fb7185" /></span>
          )}
        </div>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flex: 'none' }}>
          {/* Masked value or MISSING badge */}
          {missing ? (
            <span style={{
              fontSize: '10px', fontWeight: 700, color: '#fb7185',
              background: 'rgba(251,113,133,0.1)', border: '1px solid rgba(251,113,133,0.3)',
              padding: '2px 7px', borderRadius: '5px',
            }}>MISSING</span>
          ) : (
            <span style={{
              fontFamily: "'JetBrains Mono', monospace",
              color: '#465069',
              letterSpacing: '2px',
              fontSize: '13px',
            }}>••••••••</span>
          )}

          <button
            onClick={() => setEditing(true)}
            style={{ fontSize: '12px', fontWeight: 600, color: 'var(--accent)', background: 'none', border: 'none', cursor: 'pointer' }}
          >
            Edit
          </button>

          <button
            onClick={test}
            disabled={loading}
            style={{ fontSize: '12px', color: '#8a96ad', background: 'none', border: 'none', cursor: 'pointer' }}
          >
            {loading ? '...' : 'Test'}
          </button>

          {onDelete && (
            <button
              onClick={onDelete}
              style={{ fontSize: '12px', color: '#fb7185', background: 'none', border: 'none', cursor: 'pointer' }}
            >
              Remove
            </button>
          )}

          {testResult && (
            testResult.ok
              ? <CheckCircle size={14} color="#5fe0b4" />
              : <span title={testResult.error}><XCircle size={14} color="#fb7185" /></span>
          )}
        </div>
      )}
    </div>
  )
}
