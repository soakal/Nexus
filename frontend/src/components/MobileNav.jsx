import { useState } from 'react'
import { NavLink } from 'react-router-dom'
import { MoreHorizontal } from 'lucide-react'

const PRIMARY = ['/', '/tasks', '/chat', '/briefing']

export default function MobileNav({ nav }) {
  const [moreOpen, setMoreOpen] = useState(false)
  const primaryItems = nav.filter(n => PRIMARY.includes(n.to))
  const moreItems = nav.filter(n => !PRIMARY.includes(n.to))

  return (
    <>
      {/* Scrim */}
      {moreOpen && (
        <div
          className="md:hidden fixed inset-0 z-30 bg-black/60"
          onClick={() => setMoreOpen(false)}
        />
      )}

      {/* More slide-up sheet */}
      {moreOpen && (
        <div
          className="md:hidden fixed bottom-0 left-0 right-0 z-40 rounded-t-xl"
          style={{
            background: '#060c16',
            border: '1px solid rgba(0,212,255,0.2)',
            borderBottom: 'none',
            paddingBottom: 'calc(env(safe-area-inset-bottom) + 64px)',
          }}
        >
          <div className="p-4">
            <div className="hud-label mb-4 text-center">MORE</div>
            <div className="grid grid-cols-3 gap-3">
              {moreItems.map(({ to, icon: Icon, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  onClick={() => setMoreOpen(false)}
                  className={({ isActive }) =>
                    `flex flex-col items-center gap-1.5 p-3 rounded-lg ${
                      isActive
                        ? 'text-accent-cyan bg-accent-cyan/10'
                        : 'text-text-secondary'
                    }`
                  }
                >
                  {({ isActive }) => (
                    <>
                      <Icon
                        size={22}
                        style={isActive ? { filter: 'drop-shadow(0 0 6px rgba(0,212,255,0.7))' } : undefined}
                      />
                      <span className="text-[10px] font-mono uppercase tracking-widest leading-none text-center">
                        {label === 'Home Assistant' ? 'Home' : label}
                      </span>
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Bottom tab bar */}
      <nav
        className="md:hidden fixed bottom-0 left-0 right-0 z-40 flex items-stretch"
        style={{
          background: '#060c16',
          borderTop: '1px solid rgba(0,212,255,0.2)',
          paddingBottom: 'env(safe-area-inset-bottom)',
        }}
      >
        {primaryItems.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center py-2 gap-1 min-h-[56px] ${
                isActive ? 'text-accent-cyan' : 'text-text-secondary'
              }`
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  size={20}
                  style={isActive ? { filter: 'drop-shadow(0 0 6px rgba(0,212,255,0.7))' } : undefined}
                />
                <span className="text-[10px] font-mono uppercase tracking-widest leading-none">
                  {label}
                </span>
              </>
            )}
          </NavLink>
        ))}
        <button
          onClick={() => setMoreOpen(o => !o)}
          className={`flex-1 flex flex-col items-center justify-center py-2 gap-1 min-h-[56px] ${
            moreOpen ? 'text-accent-cyan' : 'text-text-secondary'
          }`}
        >
          <MoreHorizontal
            size={20}
            style={moreOpen ? { filter: 'drop-shadow(0 0 6px rgba(0,212,255,0.7))' } : undefined}
          />
          <span className="text-[10px] font-mono uppercase tracking-widest leading-none">More</span>
        </button>
      </nav>
    </>
  )
}
