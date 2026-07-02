import { useState, useEffect } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import SecretField from '../components/SecretField'
import CredentialCard from '../components/CredentialCard'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'
import GhostButton from '../components/GhostButton'
import TextInput from '../components/TextInput'
import { api } from '../lib/api'

function BrowserApiKey() {
  const [value, setValue] = useState(localStorage.getItem('nexus_api_key') || '')
  const [visible, setVisible] = useState(false)
  const [saved, setSaved] = useState(false)

  const save = () => {
    localStorage.setItem('nexus_api_key', value.trim())
    setSaved(true)
    setTimeout(() => window.location.reload(), 600)
  }

  return (
    <Card>
      <Eyebrow style={{ marginBottom: '12px', display: 'block' }}>Browser Authentication</Eyebrow>
      <p style={{ fontSize: '12px', color: '#8a96ad', marginBottom: '12px', margin: '0 0 12px 0' }}>
        Stored in this browser only (localStorage). Required before any other settings will load.
      </p>

      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'center' }}>
        <div style={{ position: 'relative', flex: '1 1 280px' }}>
          <TextInput
            type={visible ? 'text' : 'password'}
            value={value}
            onChange={e => { setValue(e.target.value); setSaved(false) }}
            placeholder="Paste your NEXUS API key..."
            style={{
              width: '100%',
              paddingRight: '40px',
              fontFamily: "'JetBrains Mono', monospace",
              fontSize: '13px',
              boxSizing: 'border-box',
            }}
          />
          <button
            type="button"
            onClick={() => setVisible(v => !v)}
            style={{
              position: 'absolute', right: '11px', top: '50%', transform: 'translateY(-50%)',
              background: 'none', border: 'none', cursor: 'pointer', color: '#8a96ad',
              display: 'flex', alignItems: 'center',
            }}
            title={visible ? 'Hide' : 'Show'}
          >
            {visible ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        </div>

        <PrimaryButton onClick={save} disabled={!value.trim()}>
          {saved ? 'Saved...' : 'Save'}
        </PrimaryButton>
      </div>

    </Card>
  )
}

const SECTIONS = [
  {
    title: 'AI Models',
    secrets: [
      { key: 'ANTHROPIC_API_KEY', label: 'Anthropic API Key' },
      { key: 'OPENROUTER_API_KEY', label: 'OpenRouter API Key' },
    ],
  },
  {
    title: 'Home & Network',
    secrets: [
      { key: 'HASS_TOKEN', label: 'Home Assistant Token' },
      { key: 'UNIFI_PASSWORD', label: 'UniFi Password' },
      { key: 'UNRAID_API_KEY', label: 'Unraid API Key' },
      { key: 'ADGUARD_PASS', label: 'AdGuard Password' },
    ],
  },
  {
    title: 'Media',
    secrets: [{ key: 'CHANNELS_HOST', label: 'Channels DVR Host (no auth)' }],
  },
  {
    title: 'Developer',
    secrets: [{ key: 'GITHUB_TOKEN', label: 'GitHub Personal Access Token' }],
  },
  {
    title: 'Weather',
    secrets: [{ key: 'OPENWEATHER_API_KEY', label: 'OpenWeatherMap API Key' }],
  },
  {
    title: 'Agent Bridge',
    secrets: [{ key: 'HERMES_WEBHOOK_SECRET', label: 'Hermes Webhook Secret' }],
  },
  {
    title: 'NEXUS System',
    secrets: [{ key: 'NEXUS_API_KEY', label: 'NEXUS API Key (rotates all sessions)', noDelete: true }],
  },
]

export default function Settings() {
  const [meta, setMeta] = useState({})
  const [notifyChannel, setNotifyChannel] = useState(null)
  const [credentials, setCredentials] = useState({})
  const [backupStatus, setBackupStatus] = useState(null)
  const [backingUp, setBackingUp] = useState(false)
  const [addingCred, setAddingCred] = useState(false)
  const [newCred, setNewCred] = useState({ service: '', host: '', user: '', password: '', port: '' })

  const loadMeta = () => api.secrets.list().then(r => setMeta(r?.meta || {})).catch(() => {})
  const loadCreds = () => api.secrets.credentials.list().then(r => setCredentials(r || {})).catch(() => {})

  useEffect(() => {
    loadMeta()
    loadCreds()
    api.safety.status().then(r => setNotifyChannel(r?.notify_channel || null)).catch(() => {})
  }, [])

  const notifyBroken = notifyChannel?.enabled === true && notifyChannel?.secret_present === false

  const handleDelete = async (key) => {
    if (!window.confirm(`Remove secret "${key}" from the vault?`)) return
    try {
      await api.secrets.delete(key)
      loadMeta()
    } catch (e) {
      alert(e?.message || 'Delete failed')
    }
  }

  const handleBackup = async () => {
    setBackingUp(true)
    setBackupStatus(null)
    try {
      const r = await api.secrets.backup()
      setBackupStatus(r)
    } catch (e) {
      setBackupStatus({ ok: false, error: e?.message || 'Request failed' })
    }
    setBackingUp(false)
  }

  const handleAddCred = async () => {
    if (!newCred.service.trim()) return
    try {
      await api.secrets.credentials.set(newCred)
      setNewCred({ service: '', host: '', user: '', password: '', port: '' })
      setAddingCred(false)
      loadCreds()
    } catch (e) {
      alert(e?.message || 'Save failed')
    }
  }

  return (
    <div style={{
      width: '100%',
      maxWidth: '1000px',
      margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)',
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--gap)',
    }}>
      <ScreenHeader section="Settings" title="System Configuration" />

      {notifyBroken && (
        <Card style={{
          border: '1px solid rgba(251,113,133,0.3)',
          background: 'rgba(251,113,133,0.05)',
        }}>
          <p style={{ margin: 0, fontSize: '13px', color: '#fb7185', lineHeight: '1.5' }}>
            Phone notifications are enabled but{' '}
            <span style={{ fontFamily: "'JetBrains Mono', monospace" }}>HERMES_WEBHOOK_SECRET</span>
            {' '}is missing — every alert is silently failing. Set it in the Agent Bridge section below.
          </p>
        </Card>
      )}

      <BrowserApiKey />

      {/* ── API Keys & Tokens ────────────────────────────────── */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
          <Eyebrow>API Keys &amp; Tokens</Eyebrow>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            {backupStatus && (
              <span style={{ fontSize: '11px', color: backupStatus.ok ? '#5fe0b4' : '#fb7185' }}>
                {backupStatus.ok ? `Backed up to ${backupStatus.dest}` : `Backup failed: ${backupStatus.error}`}
              </span>
            )}
            <GhostButton onClick={handleBackup} disabled={backingUp} style={{ fontSize: '12px', padding: '6px 12px' }}>
              {backingUp ? 'Backing up…' : 'Back up vault to Unraid'}
            </GhostButton>
          </div>
        </div>

        {SECTIONS.map(section => (
          <div key={section.title}>
            <div style={{ fontSize: '11px', fontWeight: 700, color: '#465069', textTransform: 'uppercase', letterSpacing: '0.08em', padding: '10px 0 2px' }}>
              {section.title}
            </div>
            {section.secrets.map(f => (
              <SecretField
                key={f.key}
                secretKey={f.key}
                label={f.label}
                lastSet={meta[f.key]?.last_set}
                missing={f.key === 'HERMES_WEBHOOK_SECRET' && notifyBroken}
                onDelete={f.noDelete ? undefined : () => handleDelete(f.key)}
                onSave={loadMeta}
              />
            ))}
          </div>
        ))}
      </Card>

      {/* ── Credentials & Passwords ──────────────────────────── */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
          <Eyebrow>Credentials &amp; Passwords</Eyebrow>
          {!addingCred && (
            <GhostButton onClick={() => setAddingCred(true)} style={{ fontSize: '12px', padding: '6px 12px' }}>
              + Add credential
            </GhostButton>
          )}
        </div>

        <p style={{ fontSize: '12px', color: '#5d6982', margin: '0 0 12px 0' }}>
          SSH and service passwords for automated deploys. Stored encrypted in the vault — never returned by any API.
        </p>

        {Object.keys(credentials).length === 0 && !addingCred && (
          <div style={{ fontSize: '13px', color: '#465069', padding: '8px 0' }}>No credentials stored yet.</div>
        )}

        {Object.entries(credentials).map(([service, data]) => (
          <CredentialCard key={service} service={service} data={data} onRefresh={loadCreds} />
        ))}

        {addingCred && (
          <div style={{ padding: '13px 0', borderTop: '1px solid rgba(120,160,220,0.07)' }}>
            <div style={{ fontSize: '13px', fontWeight: 600, color: '#dbe3f0', marginBottom: '10px' }}>New Credential</div>
            <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginBottom: '10px' }}>
              {[
                { key: 'service', label: 'Service name', placeholder: 'e.g. hermes, nas' },
                { key: 'host', label: 'Host / IP', placeholder: '192.168.1.55' },
                { key: 'user', label: 'Username', placeholder: 'root' },
                { key: 'port', label: 'Port (optional)', placeholder: '22' },
              ].map(({ key, label, placeholder }) => (
                <div key={key} style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '1 1 140px' }}>
                  <label style={{ fontSize: '11px', color: '#5d6982', fontWeight: 600 }}>{label}</label>
                  <TextInput
                    value={newCred[key]}
                    onChange={e => setNewCred(f => ({ ...f, [key]: e.target.value }))}
                    placeholder={placeholder}
                    style={{ fontSize: '13px' }}
                  />
                </div>
              ))}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: '1 1 140px' }}>
                <label style={{ fontSize: '11px', color: '#5d6982', fontWeight: 600 }}>Password</label>
                <TextInput
                  type="password"
                  value={newCred.password}
                  onChange={e => setNewCred(f => ({ ...f, password: e.target.value }))}
                  style={{ fontSize: '13px' }}
                />
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              <PrimaryButton onClick={handleAddCred} disabled={!newCred.service.trim()} style={{ padding: '7px 14px', fontSize: '12px' }}>Save</PrimaryButton>
              <GhostButton onClick={() => { setAddingCred(false); setNewCred({ service: '', host: '', user: '', password: '', port: '' }) }} style={{ padding: '7px 12px', fontSize: '12px' }}>Cancel</GhostButton>
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}
