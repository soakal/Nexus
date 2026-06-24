import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import Card from './Card'
import PrimaryButton from './PrimaryButton'
import GhostButton from './GhostButton'
import TextInput from './TextInput'
import { API_BASE } from '../lib/api'

// Mirrors Settings.jsx SECTIONS — update both if adding a new integration
const STEPS = [
  {
    title: 'AI Models',
    fields: [
      { key: 'OPENROUTER_API_KEY', label: 'OpenRouter API Key', placeholder: 'sk-or-...', hint: 'Used for Hermes and fallback models' },
    ],
  },
  {
    title: 'Home & Network',
    fields: [
      { key: 'HASS_TOKEN', label: 'Home Assistant Token', placeholder: 'eyJ...', hint: 'Long-lived access token from HA profile' },
      { key: 'UNIFI_PASSWORD', label: 'UniFi Password', placeholder: '' },
      { key: 'UNRAID_API_KEY', label: 'Unraid API Key', placeholder: '' },
      { key: 'ADGUARD_PASS', label: 'AdGuard Password', placeholder: '' },
    ],
  },
  {
    title: 'Media',
    fields: [
      { key: 'CHANNELS_HOST', label: 'Channels DVR Host', placeholder: 'http://192.168.1.x:8089', isPassword: false },
    ],
  },
  {
    title: 'Developer',
    fields: [
      { key: 'GITHUB_TOKEN', label: 'GitHub Personal Access Token', placeholder: 'ghp_...' },
    ],
  },
  {
    title: 'Weather',
    fields: [
      { key: 'OPENWEATHER_API_KEY', label: 'OpenWeatherMap API Key', placeholder: '' },
    ],
  },
  {
    title: 'Agent Bridge (Hermes)',
    fields: [
      { key: 'HERMES_WEBHOOK_SECRET', label: 'Hermes Webhook Secret', placeholder: '' },
    ],
  },
]

function SecretInput({ field, value, onChange }) {
  const isPassword = field.isPassword !== false
  const [visible, setVisible] = useState(false)
  return (
    <div style={{ marginBottom: '14px' }}>
      <label style={{ display: 'block', fontSize: '11px', color: '#5d6982', fontWeight: 600, letterSpacing: '0.1em', marginBottom: '6px' }}>
        {field.label.toUpperCase()}
      </label>
      {field.hint && (
        <div style={{ fontSize: '11px', color: '#465069', marginBottom: '5px' }}>{field.hint}</div>
      )}
      <div style={{ position: 'relative' }}>
        <TextInput
          type={isPassword && !visible ? 'password' : 'text'}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={field.placeholder || 'Leave blank to skip'}
          style={{ width: '100%', fontFamily: "'JetBrains Mono', monospace", fontSize: '13px', boxSizing: 'border-box', paddingRight: isPassword ? '38px' : undefined }}
        />
        {isPassword && (
          <button type="button" onClick={() => setVisible(v => !v)} style={{ position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)', background: 'none', border: 'none', cursor: 'pointer', color: '#8a96ad', display: 'flex', alignItems: 'center' }}>
            {visible ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        )}
      </div>
    </div>
  )
}

export default function SetupWizard() {
  const [anthropicKey, setAnthropicKey] = useState('')
  const [step, setStep] = useState(0) // 0 = Anthropic, 1..N = STEPS
  const [secrets, setSecrets] = useState({})
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)

  const totalSteps = STEPS.length + 1 // +1 for Anthropic step
  const currentStepIndex = step + 1    // display as 1-based

  const setField = (key, val) => setSecrets(s => ({ ...s, [key]: val }))

  const finish = async () => {
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`${API_BASE}/api/setup/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ anthropic_api_key: anthropicKey.trim(), secrets }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.error || 'Setup failed'); return }
      localStorage.setItem('nexus_api_key', data.nexus_api_key)
      setDone(true)
      setTimeout(() => window.location.reload(), 2000)
    } catch {
      setError('Connection error — is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  const isLastStep = step === STEPS.length
  const isAnthropicStep = step === 0

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'radial-gradient(1100px 560px at 80% -10%,rgba(47,212,238,0.06),transparent 60%),#070b13',
      padding: '24px',
    }}>
      <div style={{ width: '100%', maxWidth: '480px', display: 'flex', flexDirection: 'column', gap: '18px' }}>

        {/* Logo */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div style={{ width: 40, height: 40, borderRadius: '11px', background: 'linear-gradient(135deg,#2fd4ee,#2477c9)', color: '#05121a', fontWeight: 700, fontSize: '20px', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 6px 18px rgba(47,212,238,0.28)' }}>N</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: '16px', letterSpacing: '0.04em' }}>NEXUS</div>
            <div style={{ fontSize: '10px', letterSpacing: '0.22em', color: '#5d6982', fontWeight: 600 }}>FIRST-RUN SETUP</div>
          </div>
        </div>

        <Card>
          {done ? (
            <div style={{ padding: '12px 0' }}>
              <div style={{ color: '#34d399', fontSize: '15px', fontWeight: 600, marginBottom: '8px' }}>Setup complete</div>
              <div style={{ color: '#8a96ad', fontSize: '13px', lineHeight: 1.6 }}>
                Loading NEXUS... Background agents and scheduling will start after you run{' '}
                <span style={{ fontFamily: "'JetBrains Mono', monospace", color: '#aab4c7' }}>stop.ps1</span>
                {' '}then{' '}
                <span style={{ fontFamily: "'JetBrains Mono', monospace", color: '#aab4c7' }}>start.ps1</span>.
              </div>
            </div>
          ) : (
            <>
              {/* Step indicator */}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#dbe3f0' }}>
                  {isAnthropicStep ? 'AI Models' : STEPS[step - 1].title}
                </div>
                <div style={{ fontSize: '11px', color: '#465069' }}>Step {currentStepIndex} of {totalSteps}</div>
              </div>

              {/* Step 0: Anthropic (required) */}
              {isAnthropicStep && (
                <>
                  <p style={{ margin: '0 0 14px', fontSize: '13px', color: '#8a96ad', lineHeight: 1.6 }}>
                    Your Anthropic key is required. Everything else is optional — skip any step and add it later in Settings.
                  </p>
                  <label style={{ display: 'block', fontSize: '11px', color: '#5d6982', fontWeight: 600, letterSpacing: '0.1em', marginBottom: '6px' }}>
                    ANTHROPIC API KEY <span style={{ color: '#fb7185' }}>*</span>
                  </label>
                  <TextInput
                    type="password"
                    value={anthropicKey}
                    onChange={e => { setAnthropicKey(e.target.value); setError('') }}
                    placeholder="sk-ant-..."
                    style={{ width: '100%', fontFamily: "'JetBrains Mono', monospace", fontSize: '13px', boxSizing: 'border-box', marginBottom: error ? '8px' : '14px' }}
                    onKeyDown={e => e.key === 'Enter' && anthropicKey.trim() && setStep(1)}
                  />
                </>
              )}

              {/* Steps 1-N: integration categories */}
              {!isAnthropicStep && (
                <div>
                  {STEPS[step - 1].fields.map(field => (
                    <SecretInput
                      key={field.key}
                      field={field}
                      value={secrets[field.key] || ''}
                      onChange={val => setField(field.key, val)}
                    />
                  ))}
                </div>
              )}

              {error && (
                <p style={{ margin: '0 0 12px', fontSize: '12px', color: '#fb7185' }}>{error}</p>
              )}

              {/* Navigation */}
              <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
                {step > 0 && (
                  <GhostButton onClick={() => setStep(s => s - 1)} style={{ padding: '9px 14px', fontSize: '13px' }}>
                    Back
                  </GhostButton>
                )}

                {isAnthropicStep ? (
                  <PrimaryButton onClick={() => setStep(1)} disabled={!anthropicKey.trim()} style={{ flex: 1 }}>
                    Continue
                  </PrimaryButton>
                ) : isLastStep ? (
                  <PrimaryButton onClick={finish} disabled={loading} style={{ flex: 1 }}>
                    {loading ? 'Saving...' : 'Finish Setup'}
                  </PrimaryButton>
                ) : (
                  <>
                    <GhostButton onClick={() => setStep(s => s + 1)} style={{ padding: '9px 14px', fontSize: '13px' }}>
                      Skip
                    </GhostButton>
                    <PrimaryButton onClick={() => setStep(s => s + 1)} style={{ flex: 1 }}>
                      Next
                    </PrimaryButton>
                  </>
                )}
              </div>

              {/* Skip the rest shortcut (after step 0) */}
              {step > 0 && !isLastStep && (
                <div style={{ textAlign: 'center', marginTop: '12px' }}>
                  <button onClick={finish} disabled={loading} style={{ fontSize: '11px', color: '#465069', background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}>
                    Skip the rest &amp; finish now
                  </button>
                </div>
              )}
            </>
          )}
        </Card>

        <p style={{ fontSize: '11px', color: '#465069', textAlign: 'center', margin: 0 }}>
          A NEXUS API key is generated automatically. All secrets are encrypted on this machine only.
        </p>
      </div>
    </div>
  )
}
