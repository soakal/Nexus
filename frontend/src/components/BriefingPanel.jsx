// Inline markdown renderer — no npm packages required.

function renderInline(text) {
  // Split on **bold** first, then *italic* within plain segments.
  const boldParts = text.split(/\*\*(.+?)\*\*/g)
  const result = []
  boldParts.forEach((part, idx) => {
    if (idx % 2 === 1) {
      // Odd indices are captured bold groups
      result.push(<strong key={`b${idx}`}>{part}</strong>)
    } else {
      // Plain segment — now split on *italic*
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

function renderBody(lines) {
  const elements = []
  let listItems = []
  let listType = null // 'ul' | 'ol' | null

  function flushList(key) {
    if (listItems.length === 0) return
    if (listType === 'ul') {
      elements.push(
        <ul key={`list-${key}`} className="list-disc list-inside text-text-primary text-sm leading-relaxed space-y-0.5 ml-2">
          {listItems.map((item, i) => (
            <li key={i}>{renderInline(item)}</li>
          ))}
        </ul>
      )
    } else if (listType === 'ol') {
      elements.push(
        <ol key={`list-${key}`} className="list-decimal list-inside text-text-primary text-sm leading-relaxed space-y-0.5 ml-2">
          {listItems.map((item, i) => (
            <li key={i}>{renderInline(item)}</li>
          ))}
        </ol>
      )
    }
    listItems = []
    listType = null
  }

  lines.forEach((line, idx) => {
    const bulletMatch = line.match(/^[-*]\s+(.*)/)
    const numberedMatch = line.match(/^\d+\.\s+(.*)/)

    if (bulletMatch) {
      if (listType === 'ol') flushList(idx)
      listType = 'ul'
      listItems.push(bulletMatch[1])
    } else if (numberedMatch) {
      if (listType === 'ul') flushList(idx)
      listType = 'ol'
      listItems.push(numberedMatch[1])
    } else if (line.trim() === '') {
      flushList(idx)
      // blank line — no element emitted
    } else {
      flushList(idx)
      elements.push(
        <p key={`p-${idx}`} className="text-text-primary text-sm leading-relaxed">
          {renderInline(line)}
        </p>
      )
    }
  })

  // Flush any trailing list
  flushList('end')

  return elements
}

function renderMarkdown(content) {
  // Split on ## section headers; first segment may be pre-header preamble
  const rawSections = content.split(/^## /m)
  const sections = rawSections.filter(Boolean)

  return sections.map((section, i) => {
    const newlineIdx = section.indexOf('\n')
    const title = newlineIdx === -1 ? section.trim() : section.slice(0, newlineIdx).trim()
    const bodyText = newlineIdx === -1 ? '' : section.slice(newlineIdx + 1)
    const lines = bodyText.split('\n')
    // Trim leading/trailing blank lines
    while (lines.length && lines[0].trim() === '') lines.shift()
    while (lines.length && lines[lines.length - 1].trim() === '') lines.pop()

    return (
      <div
        key={i}
        className="hud-panel-sm p-4"
        style={{ borderLeft: '2px solid rgba(0,212,255,0.5)' }}
      >
        {title && (
          <h3 className="hud-label mb-3" style={{ color: '#00d4ff' }}>
            {title}
          </h3>
        )}
        <div className="space-y-2">
          {renderBody(lines)}
        </div>
      </div>
    )
  })
}

export default function BriefingPanel({ content }) {
  if (!content) {
    return (
      <div className="flex items-center justify-center py-12">
        <span className="hud-label">NO BRIEFING AVAILABLE</span>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {renderMarkdown(content)}
    </div>
  )
}
