import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { api } from '../lib/api'
import TextInput from './TextInput'
import PrimaryButton from './PrimaryButton'
import GhostButton from './GhostButton'

function Field({ label, value, onChange, type = 'text' }) {
  const [visible, setVisible] = useState(false)
  const isPassword = type === 'password'
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '1 1 160px' }}>
      <label style={{ fontSize: '11px', color: '#5d6982', fontWeight: 600 }}>{label}</label>
      <div style={{ position: 'relative' }}>
        <TextInput
          type={isPassword && !visible ? 'password' : 'text'}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={isPassword ? 'Leave blank to keep current' : ''}
          style={{ width: '100%', fontSize: '13px', boxSizing: 'border-box', paddingRight: isPassword ? '36px' : undefined }}
        />
        {isPassword && (
          <button
            type="button"
            onClick={() => setVisible(v => !v)}
            style={{ position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)', background: 'none', border: 'none', cursor: 'pointer', color: '#8a96ad', display: 'flex', alignItems: 'center' }}
          >
            {visible ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        )}
      </div>
    </div>
  )
}

export default function CredentialCard({ service, data, onRefresh }) {
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ host: '', user: '', password: '', port: '' })
  const [saving, setSaving] = useState(false)
  const [removing, setRemoving] = useState(false)

  const startEdit = () => {
    setForm({ host: data?.host || '', user: data?.user || '', password: '', port: data?.port || '' })
    setEditing(true)
  }

  const save = async () => {
    setSaving(true)
    try {
      await api.secrets.credentials.set({ service, ...form })
      setEditing(false)
      onRefresh()
    } catch (e) {
      alert(e?.message || 'Save failed')
    }
    setSaving(false)
  }

  const remove = async () => {
    if (!window.confirm(`Remove all credentials for "${service}"?`)) return
    setRemoving(true)
    try {
      await api.secrets.credentials.delete(service)
      onRefresh()
    } catch (e) {
      alert(e?.message || 'Remove failed')
    }
    setRemoving(false)
  }

  return (
    <div style={{
      padding: '13px 0',
      borderBottom: '1px solid rgba(120,160,220,0.07)',
    }}>
      {editing ? (
        <div>
          <div style={{ fontSize: '13px', fontWeight: 600, color: '#dbe3f0', marginBottom: '10px', textTransform: 'capitalize' }}>{service}</div>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginBottom: '10px' }}>
            <Field label="Host / IP" value={form.host} onChange={v => setForm(f => ({ ...f, host: v }))} />
            <Field label="Username" value={form.user} onChange={v => setForm(f => ({ ...f, user: v }))} />
            <Field label="Password" value={form.password} onChange={v => setForm(f => ({ ...f, password: v }))} type="password" />
            <Field label="Port (optional)" value={form.port} onChange={v => setForm(f => ({ ...f, port: v }))} />
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            <PrimaryButton onClick={save} disabled={saving} style={{ padding: '7px 14px', fontSize: '12px' }}>
              {saving ? 'Saving…' : 'Save'}
            </PrimaryButton>
            <GhostButton onClick={() => setEditing(false)} style={{ padding: '7px 12px', fontSize: '12px' }}>Cancel</GhostButton>
          </div>
        </div>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
          <div>
            <div style={{ fontSize: '14px', fontWeight: 600, color: '#dbe3f0', textTransform: 'capitalize' }}>{service}</div>
            <div style={{ fontSize: '11px', color: '#5d6982', marginTop: '2px' }}>
              {data?.host && <span>{data.host}</span>}
              {data?.user && <span style={{ marginLeft: '8px' }}>user: {data.user}</span>}
              {data?.port && <span style={{ marginLeft: '8px' }}>:{data.port}</span>}
              {data?.has_password && <span style={{ marginLeft: '8px', color: '#5fe0b4' }}>password set</span>}
            </div>
          </div>
          <div style={{ display: 'flex', gap: '14px', alignItems: 'center' }}>
            <button onClick={startEdit} style={{ fontSize: '12px', fontWeight: 600, color: 'var(--accent)', background: 'none', border: 'none', cursor: 'pointer' }}>Edit</button>
            <button onClick={remove} disabled={removing} style={{ fontSize: '12px', color: '#fb7185', background: 'none', border: 'none', cursor: 'pointer' }}>
              {removing ? '…' : 'Remove'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
