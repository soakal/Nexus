import { useState, useEffect } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import SecretField from '../components/SecretField'
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
  const [linkCopied, setLinkCopied] = useState(false)

  const save = () => {
    localStorage.setItem('nexus_api_key', value.trim())
    setSaved(true)
    setTimeout(() => window.location.reload(), 600)
  }

  const copySetupLink = async () => {
    const link = `${window.location.origin}/?key=${encodeURIComponent(value.trim())}`
    try {
      await navigator.clipboard.writeText(link)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = link
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setLinkCopied(true)
    setTimeout(() => setLinkCopied(false), 2500)
  }

  return (
    <Card>
      <Eyebrow style={{ marginBottom: '12px', display: 'block' }}>Browser Authentication</Eyebrow>
      <p style={{ fontSize: '12px', color: '#8a96ad', marginBottom: '12px', margin: '0 0 12px 0' }}>
        Stored in this browser only (localStorage). Required before any other settings will load.
      </p>

      {/* Input row */}
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

      {/* Copy setup link */}
      {value.trim() && (
        <div style={{ marginTop: '8px' }}>
          <GhostButton onClick={copySetupLink}>
            {linkCopied ? 'Link copied' : 'Copy device setup link'}
          </GhostButton>
        </div>
      )}
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
    title: 'Notes',
    secrets: [{ key: 'OBSIDIAN_TOKEN', label: 'Obsidian REST API Token' }],
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
    secrets: [{ key: 'NEXUS_API_KEY', label: 'NEXUS API Key (rotates all sessions)' }],
  },
]

export default function Settings() {
  const [meta, setMeta] = useState({})
  const [notifyChannel, setNotifyChannel] = useState(null)

  useEffect(() => {
    api.secrets.list().then(r => setMeta(r?.meta || {})).catch(() => {})
    api.safety.status().then(r => setNotifyChannel(r?.notify_channel || null)).catch(() => {})
  }, [])

  const notifyBroken = notifyChannel?.enabled === true && notifyChannel?.secret_present === false

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

      {/* Notification broken banner */}
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

      {SECTIONS.map(section => (
        <Card key={section.title}>
          <Eyebrow style={{ marginBottom: '14px', display: 'block' }}>{section.title}</Eyebrow>
          {section.secrets.map(f => (
            <SecretField
              key={f.key}
              secretKey={f.key}
              label={f.label}
              lastSet={meta[f.key]?.last_set}
              missing={f.key === 'HERMES_WEBHOOK_SECRET' && notifyBroken}
            />
          ))}
        </Card>
      ))}
    </div>
  )
}
