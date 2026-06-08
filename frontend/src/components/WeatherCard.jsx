import { Cloud, Sun, CloudRain, CloudSnow } from 'lucide-react'
const ICONS = { Clear: Sun, Rain: CloudRain, Snow: CloudSnow, Clouds: Cloud, Drizzle: CloudRain }
export default function WeatherCard({ data }) {
  if (!data) return null
  const Icon = ICONS[data.condition] || Cloud
  return (
    <div className="bg-bg-card border border-border-dark rounded-lg p-4 flex items-center gap-6">
      <Icon size={40} className="text-accent-cyan flex-shrink-0" />
      <div className="flex-1">
        <div className="font-mono text-2xl text-text-primary">{data.temp_f}°F</div>
        <div className="text-text-secondary text-sm">{data.summary}</div>
      </div>
      <div className="text-right">
        <div className="text-text-secondary text-xs">H {data.high_f}° / L {data.low_f}°</div>
        {data.precip_chance_pct > 20 && (
          <div className="text-accent-cyan text-xs mt-1">💧 {data.precip_chance_pct}% rain</div>
        )}
        <div className="text-text-secondary text-xs">Wind {data.wind_mph} mph</div>
      </div>
    </div>
  )
}
