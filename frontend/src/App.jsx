import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import {
  LayoutDashboard, FileText, CalendarDays, ListTodo, MessageSquare,
  Tv2, Home, TrendingUp, Activity, Bot, ShieldCheck, Brain,
  Settings as SettingsIcon, Menu,
} from 'lucide-react'
import StatusDot from './components/StatusDot'
import Dashboard from './pages/Dashboard'
import Briefing from './pages/Briefing'
import Today from './pages/Today'
import Tasks from './pages/Tasks'
import Chat from './pages/Chat'
import Agents from './pages/Agents'
import Media from './pages/Media'
import Trends from './pages/Trends'
import Uptime from './pages/Uptime'
import HomeAssistant from './pages/HomeAssistant'
import Settings from './pages/Settings'
import Safety from './pages/Safety'
import Facts from './pages/Facts'
import { API_BASE } from './lib/api'

const NAV = [
  { to: '/',         icon: LayoutDashboard, label: 'Dashboard',      group: 'OVERVIEW' },
  { to: '/briefing', icon: FileText,         label: 'Briefing',       group: 'OVERVIEW' },
  { to: '/today',    icon: CalendarDays,     label: 'Today',          group: 'OVERVIEW' },
  { to: '/tasks',    icon: ListTodo,         label: 'Tasks',          group: 'OVERVIEW' },
  { to: '/chat',     icon: MessageSquare,    label: 'Chat',           group: 'OVERVIEW' },
  { to: '/media',    icon: Tv2,              label: 'Media',          group: 'OVERVIEW' },
  { to: '/ha',       icon: Home,             label: 'Home Assistant', group: 'SYSTEMS'  },
  { to: '/trends',   icon: TrendingUp,       label: 'Trends',         group: 'SYSTEMS'  },
  { to: '/uptime',   icon: Activity,         label: 'Uptime',         group: 'SYSTEMS'  },
  { to: '/agents',   icon: Bot,              label: 'Agents',         group: 'SYSTEMS'  },
  { to: '/safety',   icon: ShieldCheck,      label: 'Safety',         group: 'SYSTEMS'  },
  { to: '/facts',    icon: Brain,            label: 'Facts',          group: 'SYSTEMS'  },
  { to: '/settings', icon: SettingsIcon,     label: 'Settings',       group: 'SYSTEMS'  },
]

export default function App() {
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [mobile, setMobile] = useState(typeof window !== 'undefined' ? window.innerWidth <= 880 : false)
  const [apiOk, setApiOk] = useState(true)
  const [authError, setAuthError] = useState(false)

  useEffect(() => {
    const handleResize = () => setMobile(window.innerWidth <= 880)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

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
          return
        }
        // /api/health needs no auth, so it can't tell us whether THIS browser holds
        // a valid API key. Probe one authenticated endpoint so a missing/invalid key
        // raises the "NO API KEY" banner instead of silently showing empty pages
        // (the failure mode that made the mobile Uptime page sit on "LOADING...").
        const authRes = await fetch(`${API_BASE}/api/sources/status`, {
          headers: { 'Authorization': `Bearer ${key}` },
        })
        if (authRes.status === 401) {
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

  // Sidebar style — sticky desktop, off-canvas drawer on mobile
  const navBase = {
    width: '236px',
    flex: 'none',
    boxSizing: 'border-box',
    display: 'flex',
    flexDirection: 'column',
    background: 'linear-gradient(180deg,rgba(255,255,255,0.02),rgba(255,255,255,0)),#080d16',
    borderRight: '1px solid rgba(120,160,220,0.09)',
  }
  const navShellStyle = mobile
    ? {
        ...navBase,
        position: 'fixed',
        top: 0,
        left: 0,
        height: '100vh',
        zIndex: 60,
        transform: drawerOpen ? 'translateX(0)' : 'translateX(-101%)',
        transition: 'transform .25s ease',
        boxShadow: drawerOpen ? '0 0 40px rgba(0,0,0,.55)' : 'none',
      }
    : { ...navBase, position: 'sticky', top: 0, height: '100vh' }

  // Render group labels only when the group changes
  let lastGroup = null

  return (
    <BrowserRouter>
      <div
        style={{
          '--accent': '#2fd4ee',
          '--ac-dim': 'rgba(47,212,238,0.12)',
          '--ac-line': 'rgba(47,212,238,0.32)',
          '--gap': '18px',
          '--pad': '20px',
          display: 'flex',
          minHeight: '100vh',
          background: 'radial-gradient(1100px 560px at 80% -10%,rgba(47,212,238,0.06),transparent 60%),#070b13',
        }}
      >
        {/* Mobile backdrop */}
        <div
          onClick={() => setDrawerOpen(false)}
          style={{
            display: mobile && drawerOpen ? 'block' : 'none',
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.5)',
            zIndex: 55,
          }}
        />

        {/* Sidebar */}
        <aside style={navShellStyle}>
          {/* Brand block */}
          <div
            style={{
              padding: '22px 20px 18px',
              display: 'flex',
              flexDirection: 'row',
              alignItems: 'center',
              gap: '11px',
              flex: 'none',
            }}
          >
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: '10px',
                background: 'linear-gradient(135deg,var(--accent),#2477c9)',
                color: '#05121a',
                fontWeight: 700,
                fontSize: '18px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                boxShadow: '0 6px 18px rgba(47,212,238,0.28)',
                flexShrink: 0,
              }}
            >
              N
            </div>
            <div style={{ lineHeight: 1 }}>
              <div style={{ fontWeight: 700, fontSize: '15px', letterSpacing: '0.04em' }}>
                NEXUS
              </div>
              <div
                style={{
                  fontSize: '10px',
                  letterSpacing: '0.22em',
                  color: '#5d6982',
                  fontWeight: 600,
                  marginTop: '3px',
                }}
              >
                AGENTIC OS
              </div>
            </div>
          </div>

          {/* Nav items */}
          <nav
            className="nx-nav"
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: '2px',
              padding: '6px 12px',
              flex: 1,
              overflow: 'auto',
            }}
          >
            {NAV.map((item) => {
              const showGroupLabel = item.group !== lastGroup
              lastGroup = item.group
              return (
                <div key={item.to}>
                  {showGroupLabel && (
                    <div
                      style={{
                        fontSize: '10px',
                        letterSpacing: '0.16em',
                        color: '#465069',
                        fontWeight: 700,
                        padding: '12px 12px 6px',
                      }}
                    >
                      {item.group}
                    </div>
                  )}
                  <NavLink
                    to={item.to}
                    end={item.to === '/'}
                    onClick={() => setDrawerOpen(false)}
                    style={({ isActive }) => ({
                      display: 'flex',
                      alignItems: 'center',
                      gap: '11px',
                      padding: '9px 12px',
                      borderRadius: '9px',
                      fontSize: '13px',
                      cursor: 'pointer',
                      textDecoration: 'none',
                      transition: 'background .15s,color .15s',
                      ...(isActive
                        ? {
                            color: 'var(--accent)',
                            background: 'var(--ac-dim)',
                            boxShadow: 'inset 2px 0 0 var(--accent)',
                            fontWeight: 600,
                          }
                        : { color: '#8a96ad', fontWeight: 500 }),
                    })}
                  >
                    <item.icon size={17} strokeWidth={1.7} />
                    {item.label}
                  </NavLink>
                </div>
              )
            })}
          </nav>

          {/* Footer status */}
          <div
            style={{
              padding: '14px 18px',
              borderTop: '1px solid rgba(120,160,220,0.09)',
              display: 'flex',
              flexDirection: 'row',
              alignItems: 'center',
              gap: '9px',
              flex: 'none',
            }}
          >
            <StatusDot color="#34d399" size={8} pulse />
            <span style={{ fontSize: '12px', color: '#8a96ad', fontWeight: 500 }}>
              {apiOk ? 'All systems online' : 'Systems degraded'}
            </span>
          </div>
        </aside>

        {/* Main area */}
        <main
          style={{
            flex: 1,
            minWidth: 0,
            height: '100vh',
            overflow: 'auto',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          {/* Mobile top bar */}
          <div
            style={{
              display: mobile ? 'flex' : 'none',
              alignItems: 'center',
              gap: '12px',
              padding: '0 16px',
              height: '56px',
              flex: 'none',
              position: 'sticky',
              top: 0,
              zIndex: 40,
              background: 'rgba(8,13,22,0.92)',
              backdropFilter: 'blur(10px)',
              borderBottom: '1px solid rgba(120,160,220,0.09)',
            }}
          >
            <button
              onClick={() => setDrawerOpen((o) => !o)}
              style={{
                width: 38,
                height: 38,
                borderRadius: '9px',
                border: '1px solid rgba(120,160,220,0.16)',
                background: 'rgba(255,255,255,0.03)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: 'pointer',
                color: '#aab4c7',
                flexShrink: 0,
              }}
            >
              <Menu size={19} />
            </button>
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: '8px',
                background: 'linear-gradient(135deg,var(--accent),#2477c9)',
                fontSize: '15px',
                color: '#05121a',
                fontWeight: 700,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              N
            </div>
            <span style={{ fontWeight: 700, fontSize: '14px', letterSpacing: '0.04em' }}>
              NEXUS
            </span>
            <span style={{ flex: 1 }} />
            <StatusDot color="#34d399" size={8} />
          </div>

          {/* Auth/vault warning banner */}
          {authError && (
            <div
              style={{
                background: 'rgba(251,191,36,0.12)',
                borderBottom: '1px solid rgba(251,191,36,0.4)',
                padding: '12px 20px',
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                gap: '12px',
              }}
            >
              <span className="arc-dot-warn" />
              <span className="hud-label text-accent-orange" style={{ flex: 1 }}>
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
            <Route path="/today" element={<Today />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/media" element={<Media />} />
            <Route path="/ha" element={<HomeAssistant />} />
            <Route path="/trends" element={<Trends />} />
            <Route path="/uptime" element={<Uptime />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/safety" element={<Safety />} />
            <Route path="/facts" element={<Facts />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
