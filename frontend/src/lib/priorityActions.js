// Pure parser for a briefing's "Priority Actions" section.
//
// Finds a line matching /^##\s*Priority Actions/i (tolerating a "(max 3)" suffix),
// collects lines until the next /^##\s/ heading, and splits them into bullet
// `items` (marker stripped) and free-text `note`. Missing section or empty body
// -> { items: [], note: '' }.
//
// Kept dependency-free and side-effect-free so it is trivially testable and
// reusable outside React.

const HEADING = /^##\s*Priority Actions/i
const NEXT_HEADING = /^##\s/
const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/

export function parsePriorityActions(content) {
  if (!content || typeof content !== 'string') return { items: [], note: '' }

  const lines = content.split('\n')
  let start = -1
  for (let i = 0; i < lines.length; i++) {
    if (HEADING.test(lines[i])) {
      start = i + 1
      break
    }
  }
  if (start === -1) return { items: [], note: '' }

  const items = []
  const noteParts = []
  for (let i = start; i < lines.length; i++) {
    const line = lines[i]
    if (NEXT_HEADING.test(line)) break
    if (BULLET.test(line)) {
      items.push(line.replace(BULLET, '').trim())
    } else if (line.trim()) {
      noteParts.push(line.trim())
    }
  }

  return { items, note: noteParts.join(' ').trim() }
}
