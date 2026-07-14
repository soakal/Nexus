// Inline markdown renderer (bold/italic only) — no npm packages required.
// Shared by Chat.jsx and BriefingPanel.jsx, which each render **bold**/*italic*
// text with their own surrounding list/section layout.

export function renderInline(text) {
  // Split on **bold** first, then *italic* within plain segments.
  const boldParts = text.split(/\*\*(.+?)\*\*/g)
  const result = []
  boldParts.forEach((part, idx) => {
    if (idx % 2 === 1) {
      result.push(<strong key={`b${idx}`} style={{ color: '#e9eef8' }}>{part}</strong>)
    } else {
      const italicParts = part.split(/\*(.+?)\*/g)
      italicParts.forEach((iPart, iIdx) => {
        if (iIdx % 2 === 1) {
          result.push(<em key={`i${idx}-${iIdx}`}>{iPart}</em>)
        } else if (iPart) {
          result.push(iPart)
        }
      })
    }
  })
  return result
}
