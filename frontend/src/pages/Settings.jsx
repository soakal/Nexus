import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import SecretField from '../components/SecretField'

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
      // Fallback for browsers/contexts without the async clipboard API
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
    <div className="hud-panel p-4" style={{ boxShadow: '0 0 12px rgba(0,212,255,0.25), inset 0 0 20px rgba(0,212,255,0.04)' }}>
      <h2 className="hud-label mb-1">Browser Authentication</h2>
      <p className="text-text-secondary text-xs mb-3">
        Stored in this browser only (localStorage). Required before any other settings will load.
      </p>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <input
            type={visible ? 'text' : 'password'}
            value={value}
            onChange={e => { setValue(e.target.value); setSaved(false) }}
            placeholder="Paste your NEXUS API key..."
            className="hud-input w-full pr-8"
          />
          <button
            type="button"
            onClick={() => setVisible(v => !v)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-secondary hover:text-accent-cyan"
            title={visible ? 'Hide' : 'Show'}
          >
            {visible ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        </div>
        <button
          onClick={save}
          disabled={!value.trim()}
          className="glow-btn disabled:opacity-40"
        >
          Save
        </button>
      </div>
      {saved && (
        <div className="flex items-center gap-2 mt-2">
          <span className="arc-dot" />
          <span className="text-accent-green text-xs">Saved. Reloading...</span>
        </div>
      )}
      {value.trim() && (
        <div className="mt-3 pt-3" style={{ borderTop: '1px solid rgba(0,212,255,0.12)' }}>
          <p className="text-text-secondary text-xs mb-2">
            Add another device (phone/tablet): copy this one-time setup link, open it on that device, and it auto-configures.
          </p>
          <button onClick={copySetupLink} className="glow-btn text-xs px-3 py-1.5">
            {linkCopied ? 'LINK COPIED ✓' : 'COPY DEVICE SETUP LINK'}
          </button>
        </div>
      )}
    </div>
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
  return (
    <div className="p-4 md:p-6 max-w-2xl">
      <h1 className="page-header mb-6">SYSTEM CONFIGURATION</h1>
      <div className="space-y-6">
        <BrowserApiKey />
        {SECTIONS.map(section => (
          <div key={section.title} className="hud-panel p-4">
            <h2 className="hud-label mb-3">{section.title}</h2>
            {section.secrets.map(s => (
              <SecretField key={s.key} secretKey={s.key} label={s.label} />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
