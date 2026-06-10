import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { useState, useEffect } from 'react'
import Dashboard from './pages/Dashboard'
import Briefing from './pages/Briefing'
import Tasks from './pages/Tasks'
import Voice from './pages/Voice'
import Agents from './pages/Agents'
import Media from './pages/Media'
import Trends from './pages/Trends'
import HomeAssistant from './pages/HomeAssistant'
import Settings from './pages/Settings'
import { LayoutDashboard, FileText, ListTodo, Mic, Bot, Tv2, TrendingUp, Home, Settings as SettingsIcon } from 'lucide-react'
import { API_BASE } from './lib/api'

const NAV = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/briefing', icon: FileText, label: 'Briefing' },
  { to: '/tasks', icon: ListTodo, label: 'Tasks' },
  { to: '/voice', icon: Mic, label: 'Voice' },
  { to: '/media', icon: Tv2, label: 'Media' },
  { to: '/ha', icon: Home, label: 'Home Assistant' },
  { to: '/trends', icon: TrendingUp, label: 'Trends' },
  { to: '/agents', icon: Bot, label: 'Agents' },
  { to: '/settings', icon: SettingsIcon, label: 'Settings' },
]

export default function App() {
  const [expanded, setExpanded] = useState(true)
  const [apiOk, setApiOk] = useState(true)
  const [authError, setAuthError] = useState(false)

  useEffect(() => {
    const checkHealth = async () => {
      try {
        const key = localStorage.getItem('nexus_api_key') || ''
        const res = await fetch(`${API_BASE}/api/health`, {
          headers: { 'Authorization': `Bearer ${key}` },
        })
        if (res.status === 401) {
          setAuthError(true)
          setApiOk(false)
          return
        }
        if (!res.ok) {
          setApiOk(false)
          setAuthError(false)
          return
        }
        const data = await res.json().catch(() => null)
        if (data && (data.status === 'vault_missing' || data.status === 'vault_empty')) {
          setAuthError(true)
          setApiOk(false)
        } else {
          setApiOk(true)
          setAuthError(false)
        }
      } catch {
        setApiOk(false)
        setAuthError(false)
      }
    }

    checkHealth()
    const interval = setInterval(checkHealth, 30000)
    return () => clearInterval(interval)
  }, [])

  return (
    <BrowserRouter>
      <div className="flex h-screen overflow-hidden bg-bg-primary">
        {/* Sidebar */}
        <nav
          className={`relative flex flex-col bg-bg-secondary transition-all duration-200 ${expanded ? 'w-56' : 'w-16'}`}
          style={{ borderRight: '1px solid rgba(0,212,255,0.15)' }}
        >
          {/* Brand indicator: vertical accent line */}
          <div
            className="absolute left-0 top-0 bottom-0 w-px"
            style={{
              background: 'linear-gradient(to bottom, rgba(0,212,255,0.6), rgba(0,212,255,0.05))',
              boxShadow: '0 0 8px rgba(0,212,255,0.4)',
            }}
          />

          {/* Logo area */}
          <div
            className="flex items-center p-4 cursor-pointer"
            style={{ borderBottom: '1px solid rgba(0,212,255,0.15)' }}
            onClick={() => setExpanded(e => !e)}
          >
            <div
              className="flex items-center justify-center text-bg-primary font-bold text-base"
              style={{
                width: 36,
                height: 36,
                backgroundColor: '#00d4ff',
                fontFamily: 'Orbitron, sans-serif',
                clipPath: 'polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%)',
                boxShadow: '0 0 12px rgba(0,212,255,0.6)',
              }}
            >
              N
            </div>
            {expanded && (
              <div className="ml-3 leading-none">
                <span
                  className="glow-cyan-text text-accent-cyan font-bold tracking-widest text-base"
                  style={{ fontFamily: 'Orbitron, sans-serif' }}
                >
                  NEXUS
                </span>
                <div className="hud-label mt-0.5">AGENTIC OS</div>
              </div>
            )}
          </div>

          {/* Nav items */}
          <div className="flex-1 py-4 overflow-y-auto">
            {NAV.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  `relative flex items-center px-3 py-2.5 transition-colors ${
                    isActive
                      ? 'text-accent-cyan border-l-2 border-accent-cyan'
                      : 'text-text-secondary border-l-2 border-transparent hover:text-text-primary hover:bg-accent-blue/20'
                  }`
                }
                style={({ isActive }) =>
                  isActive ? { background: 'rgba(0,212,255,0.08)' } : undefined
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon
                      size={16}
                      className={expanded ? 'mr-3' : ''}
                      style={isActive ? { filter: 'drop-shadow(0 0 6px rgba(0,212,255,0.7))' } : undefined}
                    />
                    {expanded && (
                      <span className="uppercase text-xs tracking-widest font-mono">{label}</span>
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </div>

          {/* Footer status */}
          <div className="mt-auto pb-4 px-3">
            <div
              className="mb-3"
              style={{ borderTop: '1px solid rgba(0,212,255,0.12)' }}
            />
            {expanded && (
              <div className="flex items-center">
                <span className={apiOk ? 'arc-dot' : 'arc-dot-err'} />
                <span className="hud-label ml-2">
                  {apiOk ? 'SYSTEMS ONLINE' : 'SYSTEMS DEGRADED'}
                </span>
              </div>
            )}
          </div>
        </nav>

        {/* Main content */}
        <main className="relative flex-1 overflow-y-auto bg-bg-primary">
          {/* Auth/vault warning banner */}
          {authError && (
            <div
              className="flex items-center gap-3 px-5 py-3"
              style={{
                background: 'rgba(255,149,0,0.12)',
                borderBottom: '1px solid rgba(255,149,0,0.4)',
              }}
            >
              <span className="arc-dot-warn" />
              <span className="hud-label text-accent-orange flex-1">
                NO API KEY — AUTHENTICATION REQUIRED
              </span>
              <NavLink to="/settings" className="glow-btn-gold px-3 py-1 text-xs">
                OPEN SETTINGS
              </NavLink>
            </div>
          )}

          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/briefing" element={<Briefing />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/voice" element={<Voice />} />
            <Route path="/media" element={<Media />} />
            <Route path="/ha" element={<HomeAssistant />} />
            <Route path="/trends" element={<Trends />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
