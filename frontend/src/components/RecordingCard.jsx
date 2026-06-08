export default function RecordingCard({ recording }) {
  return (
    <div className="bg-bg-secondary border border-border-dark rounded p-3">
      <div className="flex items-center gap-2">
        <span className="w-2 h-2 rounded-full bg-accent-orange animate-pulse" />
        <span className="text-text-primary text-sm font-medium">{recording.title}</span>
      </div>
      <div className="text-text-secondary text-xs mt-1">{recording.channel}</div>
    </div>
  )
}
