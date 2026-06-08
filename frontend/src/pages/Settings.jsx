import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import SecretField from '../components/SecretField'

function BrowserApiKey() {
  const [value, setValue] = useState(localStorage.getItem('nexus_api_key') || '')
  const [visible, setVisible] = useState(false)
  const [saved, setSaved] = useState(false)

  const save = () => {
    localStorage.setItem('nexus_api_key', value.trim())
    setSaved(true)
    window.location.reload()
  }

  return (
    <div className="bg-bg-card border border-accent-cyan rounded-lg p-4">
      <h2 className="text-text-secondary text-xs uppercase tracking-wider mb-1">Browser Authentication</h2>
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
            className="w-full bg-bg-secondary border border-border-dark rounded px-2 py-1.5 pr-8 text-sm text-text-primary font-mono"
          />
          <button
            type="button"
            onClick={() => setVisible(v => !v)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-secondary hover:text-text-primary"
            title={visible ? 'Hide' : 'Show'}
          >
            {visible ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        </div>
        <button
          onClick={save}
          disabled={!value.trim()}
          className="bg-accent-cyan text-bg-primary text-sm font-mono px-4 py-1.5 rounded disabled:opacity-40"
        >
          Save
        </button>
      </div>
      {saved && <div className="text-accent-green text-xs mt-2">Saved. Reloading...</div>}
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
    <div className="p-6 max-w-2xl">
      <h1 className="font-mono text-accent-cyan text-xl font-bold mb-6">SETTINGS & SECRETS</h1>
      <div className="space-y-6">
        <BrowserApiKey />
        {SECTIONS.map(section => (
          <div key={section.title} className="bg-bg-card border border-border-dark rounded-lg p-4">
            <h2 className="text-text-secondary text-xs uppercase tracking-wider mb-3">{section.title}</h2>
            {section.secrets.map(s => (
              <SecretField key={s.key} secretKey={s.key} label={s.label} />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
