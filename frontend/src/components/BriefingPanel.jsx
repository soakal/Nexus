// Inline markdown renderer — no npm packages required.

function renderInline(text) {
  // Split on **bold** first, then *italic* within plain segments.
  const boldParts = text.split(/\*\*(.+?)\*\*/g)
  const result = []
  boldParts.forEach((part, idx) => {
    if (idx % 2 === 1) {
      // Odd indices are captured bold groups
      result.push(<strong key={`b${idx}`} style={{ color: '#e9eef8' }}>{part}</strong>)
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

  const listStyle = {
    color: '#aab4c7',
    fontSize: 13,
    lineHeight: 1.6,
    margin: 0,
    paddingLeft: 20,
  }

  function flushList(key) {
    if (listItems.length === 0) return
    if (listType === 'ul') {
      elements.push(
        <ul key={`list-${key}`} style={listStyle}>
          {listItems.map((item, i) => (
            <li key={i}>{renderInline(item)}</li>
          ))}
        </ul>
      )
    } else if (listType === 'ol') {
      elements.push(
        <ol key={`list-${key}`} style={listStyle}>
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
        <p key={`p-${idx}`} style={{ color: '#aab4c7', fontSize: 13, lineHeight: 1.6, margin: 0 }}>
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
        style={{
          background: 'linear-gradient(180deg,rgba(255,255,255,0.025),rgba(255,255,255,0)),#0c1320',
          border: '1px solid rgba(120,160,220,0.10)',
          borderRadius: 16,
          padding: 'var(--pad)',
        }}
      >
        {title && (
          <div style={{
            fontSize: 11,
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
            color: '#5d6982',
            fontWeight: 600,
            marginBottom: 14,
          }}>
            {title}
          </div>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {renderBody(lines)}
        </div>
      </div>
    )
  })
}

export default function BriefingPanel({ content }) {
  if (!content) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '48px 0' }}>
        <span style={{ color: '#5d6982', fontSize: 13 }}>No briefing available</span>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
      {renderMarkdown(content)}
    </div>
  )
}
