import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import Card from '../components/Card'
import Eyebrow from '../components/Eyebrow'
import StatusPill from '../components/StatusPill'
import ScreenHeader from '../components/ScreenHeader'
import PrimaryButton from '../components/PrimaryButton'
import GhostButton from '../components/GhostButton'
import TextInput from '../components/TextInput'

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/

function shortSender(sender) {
  const m = /^"?([^"<]+?)"?\s*<.*$/.exec(sender || '')
  return m ? m[1] : (sender || '(unknown sender)')
}

export default function Mail() {
  const [to, setTo] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState('')
  const [sendOk, setSendOk] = useState(false)

  const [inbox, setInbox] = useState(null)
  const [inboxError, setInboxError] = useState(false)

  // Preview accordion (Traces.jsx pattern): expandedId toggles, detailById caches
  // by email_id so re-expanding a previously opened row doesn't refetch.
  const [expandedId, setExpandedId] = useState(null)
  const [detailById, setDetailById] = useState({})
  const [detailLoadingId, setDetailLoadingId] = useState(null)
  const [detailErrors, setDetailErrors] = useState({})

  // Archive/delete row actions.
  const [busyId, setBusyId] = useState(null)
  const [rowErrors, setRowErrors] = useState({})

  const loadInbox = useCallback(() => {
    api.protonmail.inbox()
      .then(d => { setInbox(d); setInboxError(false) })
      .catch(() => setInboxError(true))
  }, [])

  useEffect(() => {
    loadInbox()
    const onVis = () => { if (!document.hidden) loadInbox() }
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
    }
  }, [loadInbox])

  const recipients = to.split(',').map(r => r.trim()).filter(Boolean)
  const recipientsValid = recipients.length > 0 && recipients.every(r => EMAIL_RE.test(r))
  const canSend = recipientsValid && subject.trim() && body.trim() && !sending

  const send = async () => {
    if (!canSend) return
    if (!window.confirm(`Send email to ${recipients.join(', ')}?`)) return
    setSending(true)
    setSendError('')
    setSendOk(false)
    try {
      await api.protonmail.send({ recipients, subject: subject.trim(), body: body.trim() })
      setSendOk(true)
      setTo(''); setSubject(''); setBody('')
      loadInbox()
    } catch (e) {
      setSendError(e?.message || 'Send failed.')
    } finally {
      setSending(false)
    }
  }

  async function toggleExpand(emailId) {
    if (expandedId === emailId) {
      setExpandedId(null)
      return
    }
    setExpandedId(emailId)
    if (detailById[emailId]) return
    setDetailLoadingId(emailId)
    try {
      const detail = await api.protonmail.email(emailId)
      setDetailById(prev => ({ ...prev, [emailId]: detail }))
    } catch (err) {
      setDetailErrors(prev => ({ ...prev, [emailId]: err?.message || 'Failed to load email.' }))
    } finally {
      setDetailLoadingId(null)
    }
  }

  function removeRow(emailId) {
    setInbox(prev => prev ? { ...prev, emails: (prev.emails || []).filter(e => e.email_id !== emailId) } : prev)
    if (expandedId === emailId) setExpandedId(null)
  }

  async function archiveEmail(e, row) {
    e.stopPropagation()
    setBusyId(row.email_id)
    setRowErrors(prev => ({ ...prev, [row.email_id]: '' }))
    try {
      await api.protonmail.archive({ email_id: row.email_id })
      removeRow(row.email_id)
      loadInbox()
    } catch (err) {
      setRowErrors(prev => ({ ...prev, [row.email_id]: err?.message || 'Archive failed.' }))
    } finally {
      setBusyId(null)
    }
  }

  async function deleteEmail(e, row) {
    // No confirm — moves to Trash (reversible), same risk band as Archive.
    e.stopPropagation()
    setBusyId(row.email_id)
    setRowErrors(prev => ({ ...prev, [row.email_id]: '' }))
    try {
      await api.protonmail.remove({ email_id: row.email_id })
      removeRow(row.email_id)
      loadInbox()
    } catch (err) {
      setRowErrors(prev => ({ ...prev, [row.email_id]: err?.message || 'Delete failed.' }))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div style={{ width: '100%', maxWidth: '900px', margin: '0 auto', padding: 'clamp(16px,3vw,32px)', display: 'flex', flexDirection: 'column', gap: 'var(--gap)' }}>
      <ScreenHeader section="Mail" title="Proton Mail" />

      <Card style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        <Eyebrow>Compose</Eyebrow>
        <TextInput placeholder="To (comma-separated addresses)" value={to} onChange={e => setTo(e.target.value)} />
        <TextInput placeholder="Subject" value={subject} onChange={e => setSubject(e.target.value)} />
        <textarea
          placeholder="Body"
          value={body}
          onChange={e => setBody(e.target.value)}
          rows={8}
          style={{ padding: '12px 14px', borderRadius: '11px', border: '1px solid rgba(120,160,220,0.16)',
            background: 'rgba(255,255,255,0.03)', color: '#e9eef8', fontSize: '14px', outline: 'none',
            fontFamily: 'inherit', resize: 'vertical' }}
        />
        {to && !recipientsValid && (
          <div style={{ fontSize: '12px', color: '#f87171' }}>One or more recipient addresses look invalid.</div>
        )}
        {sendError && <div style={{ fontSize: '12px', color: '#f87171' }}>{sendError}</div>}
        {sendOk && <div style={{ fontSize: '12px', color: '#34d399' }}>Sent.</div>}
        <div>
          <PrimaryButton onClick={send} disabled={!canSend}>
            {sending ? 'Sending…' : 'Send'}
          </PrimaryButton>
        </div>
      </Card>

      <Card style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Eyebrow>Recent</Eyebrow>
          {inboxError ? (
            <StatusPill tone="red" label="Offline" />
          ) : inbox ? (
            <StatusPill tone={inbox.unread > 0 ? 'amber' : 'green'} label={inbox.unread > 0 ? `${inbox.unread} unread` : 'Caught up'} />
          ) : null}
        </div>
        {!inboxError && (inbox?.emails || []).map(e => (
          <div key={e.email_id}>
            <div
              onClick={() => toggleExpand(e.email_id)}
              style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(120,160,220,0.08)', cursor: 'pointer' }}
            >
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                <span style={{ fontSize: '13px', color: '#cdd6e6', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {shortSender(e.sender)}
                </span>
                <span style={{ fontSize: '12px', color: '#8a96ad', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {e.subject || '(no subject)'}
                </span>
              </div>
              <GhostButton onClick={(ev) => archiveEmail(ev, e)} disabled={busyId === e.email_id}>
                Archive
              </GhostButton>
              <GhostButton onClick={(ev) => deleteEmail(ev, e)} disabled={busyId === e.email_id} style={{ color: '#f87171' }}>
                Delete
              </GhostButton>
            </div>

            {rowErrors[e.email_id] && (
              <div style={{ fontSize: '11px', color: '#f87171', margin: '2px 0 0 12px' }}>{rowErrors[e.email_id]}</div>
            )}

            {expandedId === e.email_id && (
              <div style={{ margin: '6px 0 4px 12px', padding: '10px 12px', borderRadius: '10px', background: 'rgba(255,255,255,0.015)', border: '1px solid rgba(120,160,220,0.06)' }}>
                {detailLoadingId === e.email_id ? (
                  <span style={{ fontSize: '12px', color: '#5d6982' }}>Loading…</span>
                ) : detailErrors[e.email_id] ? (
                  <span style={{ fontSize: '12px', color: '#f87171' }}>{detailErrors[e.email_id]}</span>
                ) : detailById[e.email_id] ? (
                  <>
                    <div style={{ fontSize: '11px', color: '#5d6982', marginBottom: '8px' }}>
                      {detailById[e.email_id].date}
                    </div>
                    <pre style={{
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'inherit',
                      fontSize: '13px', color: '#dbe3f0', margin: 0,
                      maxHeight: '360px', overflowY: 'auto',
                    }}>
                      {detailById[e.email_id].body || '(empty body)'}
                    </pre>
                  </>
                ) : null}
              </div>
            )}
          </div>
        ))}
        {!inboxError && (inbox?.emails?.length || 0) === 0 && (
          <div style={{ fontSize: '12px', color: '#5d6982' }}>No emails.</div>
        )}
      </Card>
    </div>
  )
}
