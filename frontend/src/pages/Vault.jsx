import { useState, useEffect, useMemo, useRef } from 'react'
import ForceGraph from 'force-graph'
import { api } from '../lib/api'
import Card from '../components/Card'
import ScreenHeader from '../components/ScreenHeader'
import TextInput from '../components/TextInput'
import GhostButton from '../components/GhostButton'
import Eyebrow from '../components/Eyebrow'
import { renderInline } from '../lib/markdown'

// ---------------------------------------------------------------------------
// Markdown-lite note body (headings / bullets / fences / wikilinks).
// renderInline (lib/markdown.jsx) stays bold/italic-only and shared; the
// block layer lives here because only this page views whole notes.
// ---------------------------------------------------------------------------

function WikiText({ text, onOpen, resolve }) {
  const parts = text.split(/\[\[([^\]]+)\]\]/g)
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const [rawTarget, alias] = part.split('|')
      const target = rawTarget.split('#')[0].trim()
      const file = resolve(target)
      return file ? (
        <a key={i} onClick={() => onOpen(`wiki/${file}`)}
           style={{ color: 'var(--accent)', cursor: 'pointer' }}>{alias || target}</a>
      ) : (
        <span key={i} style={{ color: '#98958c' }}>{alias || target}</span>
      )
    }
    return part ? <span key={i}>{renderInline(part)}</span> : null
  })
}

function NoteBody({ content, onOpen, resolve }) {
  const blocks = []
  let inFence = false
  let fence = []
  content.split('\n').forEach((line, i) => {
    if (line.trimStart().startsWith('```')) {
      if (inFence) {
        blocks.push(
          <pre key={`f${i}`} style={{ background: 'rgba(255,255,255,0.04)', borderRadius: '4px',
            padding: '12px', overflowX: 'auto', fontSize: '12.5px' }}>{fence.join('\n')}</pre>
        )
        fence = []
      }
      inFence = !inFence
      return
    }
    if (inFence) { fence.push(line); return }
    const h = line.match(/^(#{1,4})\s+(.*)/)
    if (h) {
      blocks.push(
        <div key={i} style={{ fontWeight: 700, color: '#f4f3f0', marginTop: '14px',
          fontSize: `${19 - h[1].length * 1.5}px` }}>
          <WikiText text={h[2]} onOpen={onOpen} resolve={resolve} />
        </div>
      )
      return
    }
    const b = line.match(/^\s*[-*]\s+(.*)/)
    if (b) {
      blocks.push(
        <div key={i} style={{ paddingLeft: '18px' }}>
          <span style={{ color: '#7a776d' }}>&bull;&nbsp;</span>
          <WikiText text={b[1]} onOpen={onOpen} resolve={resolve} />
        </div>
      )
      return
    }
    if (line.trim() === '') { blocks.push(<div key={i} style={{ height: '8px' }} />); return }
    blocks.push(<div key={i}><WikiText text={line} onOpen={onOpen} resolve={resolve} /></div>)
  })
  return <div style={{ fontSize: '14px', lineHeight: 1.65, color: '#d9d6cd' }}>{blocks}</div>
}

// vault_search returns "**relpath**\ncontext" blocks joined by \n\n
// (backend/integrations/obsidian.py -- format is ours, parsed leniently).
function parseSearchResult(str) {
  if (!str || str.startsWith('No notes') || str.startsWith('Vault search unavailable')) return []
  return str.split('\n\n').map((block) => {
    const lines = block.split('\n')
    const m = lines[0].match(/^\*\*(.+)\*\*$/)
    return { relpath: m ? m[1] : lines[0], context: lines.slice(1).join(' ') }
  })
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Vault() {
  const [tab, setTab] = useState('browse')           // 'browse' | 'search' | 'graph'
  const [catalog, setCatalog] = useState(null)       // null until loaded
  const [filter, setFilter] = useState('')
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [results, setResults] = useState(null)       // null = not searched yet
  const [rawResult, setRawResult] = useState('')
  const [note, setNote] = useState(null)             // { path, content }
  const [error, setError] = useState('')

  // Narrow-viewport (phone) layout: the list panel and note viewer can't sit
  // side by side below ~720px, and the list panel alone claims up to 65vh --
  // stacking them (the default flex-wrap behavior) buries an opened note
  // below the fold with no visible sign anything happened. Below the
  // breakpoint, show ONLY the list or ONLY the viewer (master-detail),
  // switching on whether a note is open, instead of ever stacking both.
  const [isNarrow, setIsNarrow] = useState(() => window.innerWidth < 720)
  useEffect(() => {
    const onResize = () => setIsNarrow(window.innerWidth < 720)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Graph state: raw fetch is separate from the degree-filter threshold, so
  // adjusting the slider re-filters in place instead of re-fetching.
  const [graphData, setGraphData] = useState(null)   // { nodes, links } from the API
  const [minDegree, setMinDegree] = useState(0)       // clutter filter -- see below
  const graphRef = useRef(null)
  const fgRef = useRef(null)

  useEffect(() => {
    api.vault.catalog().then(setCatalog).catch((e) => setError(String(e)))
  }, [])

  // Client-side wikilink resolution: same rung order as the backend resolver
  // (stem exact -> title exact -> ci stem -> ci title), returning the filename.
  const resolve = useMemo(() => {
    const pages = catalog?.pages || []
    const byStem = {}, byTitle = {}, byStemCi = {}, byTitleCi = {}
    pages.forEach((p) => {
      if (!p.filename) return
      const stem = p.filename.replace(/\.md$/, '')
      if (!(stem in byStem)) byStem[stem] = p.filename
      if (!(stem.toLowerCase() in byStemCi)) byStemCi[stem.toLowerCase()] = p.filename
      if (p.title && !(p.title in byTitle)) byTitle[p.title] = p.filename
      if (p.title && !(p.title.toLowerCase() in byTitleCi)) byTitleCi[p.title.toLowerCase()] = p.filename
    })
    return (target) =>
      byStem[target] ?? byTitle[target] ?? byStemCi[target.toLowerCase()] ?? byTitleCi[target.toLowerCase()] ?? null
  }, [catalog])

  function openNote(path) {
    setError('')
    api.vault.note(path).then(setNote).catch((e) => setError(String(e)))
  }

  async function runSearch() {
    if (!query.trim() || searching) return
    setSearching(true)
    setError('')
    try {
      const { result } = await api.vault.search(query.trim())
      setRawResult(result)
      setResults(parseSearchResult(result))
    } catch (e) {
      setError(String(e))
    } finally {
      setSearching(false)
    }
  }

  // Fetch the graph once per tab entry (not per slider move).
  useEffect(() => {
    if (tab !== 'graph' || graphData) return
    api.vault.graph().then(setGraphData).catch((e) => setError(String(e)))
  }, [tab, graphData])

  // Per-node degree (connection count), computed once from the raw fetch --
  // used both to size nodes and to drive the clutter filter below.
  const degree = useMemo(() => {
    const d = {}
    if (!graphData) return d
    graphData.links.forEach((l) => {
      d[l.source] = (d[l.source] || 0) + 1
      d[l.target] = (d[l.target] || 0) + 1
    })
    return d
  }, [graphData])

  const maxDegree = useMemo(
    () => Object.values(degree).reduce((m, v) => Math.max(m, v), 0),
    [degree]
  )

  // The clutter fix: at ~260+ nodes a full unfiltered graph is a hairball.
  // "Min connections" hides nodes below the threshold (and any link touching
  // a hidden node) -- 0 shows everything, including single-link/orphan pages.
  const filteredGraph = useMemo(() => {
    if (!graphData) return null
    if (minDegree <= 0) return graphData
    const keep = new Set(graphData.nodes.filter((n) => (degree[n.id] || 0) >= minDegree).map((n) => n.id))
    return {
      nodes: graphData.nodes.filter((n) => keep.has(n.id)),
      links: graphData.links.filter((l) => keep.has(l.source) && keep.has(l.target)),
    }
  }, [graphData, degree, minDegree])

  // Zoom level past which every node's name renders on-canvas, not just the
  // hovered/selected one. Tuned so a fully zoomed-out 260-node graph stays
  // dots-only (no label soup); zooming in toward a cluster reveals names
  // progressively. globalScale is force-graph's own pan/zoom multiplier, so
  // this threshold is scale-relative, not tied to any fixed pixel count.
  const LABEL_ZOOM_THRESHOLD = 2.4
  // Plain refs, not state: these are read inside the canvas draw callback on
  // every animation frame, and must never trigger a React re-render on mouse
  // move -- only the imperative force-graph instance needs to know they
  // changed, via its own hover-triggered repaint.
  const hoveredIdRef = useRef(null)
  const selectedIdRef = useRef(null)

  function nodeRadius(n) {
    return 2 + Math.sqrt(degree[n.id] || 0) * 1.3
  }

  // Graph lifecycle: build the force-graph instance once when data first
  // arrives, then just call .graphData() again on re-filter -- no rebuild.
  useEffect(() => {
    if (tab !== 'graph' || !graphRef.current || !filteredGraph) return
    const el = graphRef.current
    if (!fgRef.current) {
      const fg = ForceGraph()(el)
        .width(el.clientWidth)
        .height(el.clientHeight)
        .linkColor(() => 'rgba(180,178,170,0.25)')
        .backgroundColor('rgba(0,0,0,0)')
        // Custom paint so a name can render on-canvas (at zoom, on hover, or
        // on selection) instead of only in a hover tooltip -- overriding
        // this requires also overriding nodePointerAreaPaint below, or
        // click/hover hit-testing silently stops working (force-graph uses
        // a separate off-screen hit-canvas for pointer events).
        .nodeCanvasObject((n, ctx, globalScale) => {
          const r = nodeRadius(n)
          const isFocused = n.id === hoveredIdRef.current || n.id === selectedIdRef.current
          ctx.beginPath()
          ctx.arc(n.x, n.y, r, 0, 2 * Math.PI, false)
          ctx.fillStyle = isFocused ? '#ffffff' : '#ff8a3d'
          ctx.fill()

          if (globalScale >= LABEL_ZOOM_THRESHOLD || isFocused) {
            const fontSize = Math.max(10, 12 / globalScale)
            ctx.font = `${isFocused ? '600 ' : ''}${fontSize}px sans-serif`
            ctx.textAlign = 'center'
            ctx.textBaseline = 'top'
            ctx.fillStyle = isFocused ? '#ffffff' : '#d9d6cd'
            ctx.fillText(n.title, n.x, n.y + r + 2)
          }
        })
        .nodePointerAreaPaint((n, color, ctx) => {
          ctx.fillStyle = color
          ctx.beginPath()
          ctx.arc(n.x, n.y, nodeRadius(n), 0, 2 * Math.PI, false)
          ctx.fill()
        })
        .onNodeHover((n) => {
          hoveredIdRef.current = n ? n.id : null
          el.style.cursor = n ? 'pointer' : 'default'
        })
        .onNodeClick((n) => {
          // First click on a new node selects it (shows its name, matches
          // "select" from the graph); clicking the already-selected node is
          // what actually opens it -- a bare click can't preview a name AND
          // navigate away in the same gesture, since navigating away is
          // instant. Consistent with clicking empty canvas to deselect.
          if (selectedIdRef.current === n.id) {
            openNote(`wiki/${n.filename}`)
            setTab('browse')
          } else {
            selectedIdRef.current = n.id
          }
        })
        .onBackgroundClick(() => { selectedIdRef.current = null })
      fg.__onResize = () => fg.width(el.clientWidth).height(el.clientHeight)
      window.addEventListener('resize', fg.__onResize)
      fgRef.current = fg
    }
    // nodeVal still drives simulation physics (charge/collision) even though
    // nodeCanvasObject now owns the visuals -- reads `degree` via closure, so
    // this must be reset on every filter change like the paint callback.
    fgRef.current.nodeVal((n) => 1 + (degree[n.id] || 0))
    // force-graph mutates link.source/target into node object refs the first
    // time a given links array is bound -- pass a fresh array each call so
    // re-filtering never re-mutates (and corrupts) the original graphData.
    fgRef.current.graphData({
      nodes: filteredGraph.nodes,
      links: filteredGraph.links.map((l) => ({ ...l })),
    })
  }, [tab, filteredGraph, degree])

  // Teardown on tab exit OR page unmount -- a cleanup function (not a
  // tab-watching effect body) is required for the unmount case: navigating
  // away from /vault while on the Graph tab unmounts this component without
  // ever changing `tab`, so a body-only effect never tears down the running
  // instance and its rAF animation loop leaks forever against a detached
  // canvas.
  useEffect(() => {
    if (tab !== 'graph') return
    return () => {
      if (fgRef.current) {
        window.removeEventListener('resize', fgRef.current.__onResize)
        fgRef.current._destructor()
        fgRef.current = null
      }
    }
  }, [tab])

  const pages = catalog?.pages || []
  const shown = filter.trim()
    ? pages.filter((p) => (p.title + ' ' + p.filename + ' ' + (p.headers || ''))
        .toLowerCase().includes(filter.trim().toLowerCase()))
    : pages

  const tabBtn = (id, label) => (
    <GhostButton key={id} onClick={() => setTab(id)}
      style={tab === id ? { border: '1px solid var(--ac-line)', color: 'var(--accent)', background: 'var(--ac-dim)' } : {}}>
      {label}
    </GhostButton>
  )

  const viewer = (
    <Card flex="2 1 420px" style={{ minWidth: 0 }}>
      {note ? (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
            <Eyebrow>{note.path}</Eyebrow>
            <GhostButton onClick={() => setNote(null)}>{isNarrow ? '← Back' : 'Close'}</GhostButton>
          </div>
          <NoteBody content={note.content} onOpen={openNote} resolve={resolve} />
        </>
      ) : (
        <div style={{ color: '#7a776d', fontSize: '13px' }}>Select a note to view it here.</div>
      )}
    </Card>
  )

  return (
    <div style={{ width: '100%', maxWidth: '1200px', margin: '0 auto',
      padding: 'clamp(16px,3vw,32px)', display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
      <ScreenHeader section="Vault" title="Knowledge Vault"
        subline={catalog ? `${pages.length} wiki pages · catalog built ${catalog.built_at || 'n/a'}` : 'Loading catalog…'} />

      <div style={{ display: 'flex', gap: '8px' }}>
        {tabBtn('browse', 'Browse')}
        {tabBtn('search', 'Search')}
        {tabBtn('graph', 'Graph')}
      </div>

      {error && <Card accent="amber"><div style={{ fontSize: '13px', color: '#e8c468' }}>{error}</div></Card>}

      {tab === 'graph' ? (
        <Card style={{ padding: '8px' }}>
          {graphData && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '6px 10px 12px', flexWrap: 'wrap' }}>
              <Eyebrow>Min. connections</Eyebrow>
              <input type="range" min={0} max={maxDegree} value={minDegree}
                onChange={(e) => setMinDegree(Number(e.target.value))}
                style={{ width: '180px' }} />
              <span style={{ fontSize: '12.5px', color: '#98958c' }}>
                {minDegree === 0 ? 'All nodes' : `${minDegree}+`} &middot; showing{' '}
                {filteredGraph ? filteredGraph.nodes.length : 0} of {graphData.nodes.length} pages
              </span>
              {minDegree > 0 && <GhostButton onClick={() => setMinDegree(0)}>Reset</GhostButton>}
            </div>
          )}
          <div ref={graphRef} style={{ width: '100%', height: '62vh' }} />
        </Card>
      ) : (
        <div style={{ display: 'flex', gap: 'var(--gap)', alignItems: 'flex-start', flexWrap: 'wrap' }}>
          {/* Master-detail below 720px: never show both the list and the
              viewer at once -- see isNarrow's comment above for why. */}
          {(!isNarrow || !note) && <Card flex="1 1 300px" style={{ minWidth: 0 }}>
            {tab === 'browse' ? (
              <>
                <TextInput placeholder="Filter pages…" value={filter}
                  onChange={(e) => setFilter(e.target.value)} style={{ width: '100%', marginBottom: '12px' }} />
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '65vh', overflowY: 'auto' }}>
                  {catalog === null && <div style={{ color: '#7a776d', fontSize: '13px' }}>Loading…</div>}
                  {shown.map((p) => (
                    <div key={p.filename} onClick={() => openNote(`wiki/${p.filename}`)}
                      style={{ padding: '8px 10px', borderRadius: '4px', cursor: 'pointer',
                        background: note?.path === `wiki/${p.filename}` ? 'var(--ac-dim)' : 'transparent' }}>
                      <div style={{ fontSize: '13.5px', color: '#f4f3f0', fontWeight: 600 }}>{p.title}</div>
                      {p.summary && <div style={{ fontSize: '12px', color: '#98958c',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.summary}</div>}
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <>
                <div style={{ display: 'flex', gap: '8px', marginBottom: '12px' }}>
                  <TextInput placeholder="Search the vault…" value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && runSearch()} style={{ flex: 1 }} />
                  <GhostButton onClick={runSearch} disabled={searching}>{searching ? '…' : 'Search'}</GhostButton>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '65vh', overflowY: 'auto' }}>
                  {results !== null && results.length === 0 && (
                    <div style={{ color: '#7a776d', fontSize: '13px' }}>{rawResult || 'No results.'}</div>
                  )}
                  {(results || []).map((r, i) => (
                    <div key={i} onClick={() => openNote(r.relpath.replace(/^Brain\//, ''))}
                      style={{ padding: '10px', borderRadius: '4px', cursor: 'pointer',
                        border: '1px solid rgba(180,178,170,0.10)' }}>
                      <div style={{ fontSize: '13px', color: 'var(--accent)', fontWeight: 600 }}>{r.relpath}</div>
                      {r.context && <div style={{ fontSize: '12.5px', color: '#98958c', marginTop: '4px' }}>{r.context}</div>}
                    </div>
                  ))}
                </div>
              </>
            )}
          </Card>}
          {(!isNarrow || note) && viewer}
        </div>
      )}
    </div>
  )
}
