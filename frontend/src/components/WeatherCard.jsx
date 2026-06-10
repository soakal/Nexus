import { Cloud, Sun, CloudRain, CloudSnow } from 'lucide-react'
const ICONS = { Clear: Sun, Rain: CloudRain, Snow: CloudSnow, Clouds: Cloud, Drizzle: CloudRain }
export default function WeatherCard({ data }) {
  if (!data || data.error || data.temp_f == null) return null
  const Icon = ICONS[data.condition] || Cloud
  return (
    <div className="hud-panel p-5 flex items-center gap-6">
      <Icon size={44} className="text-accent-cyan flex-shrink-0" style={{ filter: 'drop-shadow(0 0 8px rgba(0,212,255,0.6))' }} />
      <div className="flex-1">
        <div className="text-text-primary glow-cyan-text" style={{ fontFamily: 'Orbitron, sans-serif', fontSize: '2rem' }}>{data.temp_f}°F</div>
        <div className="text-text-secondary text-sm">{data.summary}</div>
      </div>
      <div className="text-right space-y-1">
        <div>
          <span className="hud-label">H / L</span>
          <span className="font-mono text-text-primary text-sm ml-2">{data.high_f}° / {data.low_f}°</span>
        </div>
        {data.precip_chance_pct > 20 && (
          <div className="inline-block font-mono text-xs px-2 py-0.5 rounded-sm bg-accent-blue text-accent-cyan border border-accent-cyan/30">
            {data.precip_chance_pct}% RAIN
          </div>
        )}
        <div className="text-text-secondary text-xs font-mono">WIND {data.wind_mph} MPH</div>
      </div>
    </div>
  )
}
