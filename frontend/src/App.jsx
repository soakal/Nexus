import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { useState } from 'react'
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
  return (
    <BrowserRouter>
      <div className="flex h-screen overflow-hidden bg-bg-primary">
        {/* Sidebar */}
        <nav className={`flex flex-col bg-bg-secondary border-r border-border-dark transition-all duration-200 ${expanded ? 'w-56' : 'w-16'}`}>
          <div className="flex items-center p-4 border-b border-border-dark cursor-pointer" onClick={() => setExpanded(e => !e)}>
            <div className="w-8 h-8 rounded-md bg-accent-cyan flex items-center justify-center text-bg-primary font-mono font-bold text-sm">N</div>
            {expanded && <span className="ml-3 font-mono text-accent-cyan font-bold tracking-widest text-sm">NEXUS</span>}
          </div>
          <div className="flex-1 py-4 overflow-y-auto">
            {NAV.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  `flex items-center px-4 py-3 text-sm transition-colors ${
                    isActive
                      ? 'text-accent-cyan bg-accent-cyan/10 border-r-2 border-accent-cyan'
                      : 'text-text-secondary hover:text-text-primary hover:bg-white/5'
                  }`
                }
              >
                <Icon size={18} />
                {expanded && <span className="ml-3">{label}</span>}
              </NavLink>
            ))}
          </div>
        </nav>
        {/* Main content */}
        <main className="flex-1 overflow-y-auto bg-bg-primary">
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
