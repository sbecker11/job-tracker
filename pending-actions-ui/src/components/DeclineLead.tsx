import { useState } from 'react'
import { gmailComposeUrl, helperQuery } from '../lib/links'
import type { ContactPriorityItem } from '../priorityQueue'
import type { DeclineReason } from '../types'

export type DeclineReasonOption = DeclineReason

function declineUrl(
  key: string,
  reason: string,
  note: string,
  opts: { channel?: string; letter: boolean },
): string {
  return `declinelead://decline?${helperQuery({
    key,
    reason,
    note: note.trim(),
    channel: opts.channel,
    letter: opts.letter ? undefined : '0',
  })}`
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    /* clipboard can be blocked in the embedded browser; the preview is selectable */
  }
}

interface Props {
  item: ContactPriorityItem
  reasons: DeclineReasonOption[]
  closing: string
  /** Called after either close link is clicked (the helper app does the write). */
  onClosed?: () => void
}

/** "Decline Lead" form — draft a "no thank you" letter for one of the canned
 * reasons, send it yourself (copy / thread / Gmail compose), then close the
 * lead as skipped with that reason via `declinelead://` (tools/decline-lead/ →
 * `decline-lead`). Leads with no recruiter get "Skip without letter". */
export function DeclineLeadForm({ item, reasons, closing, onClosed }: Props) {
  const [code, setCode] = useState(reasons[0]?.code || 'not_qualified')
  const [note, setNote] = useState('')
  const [copied, setCopied] = useState(false)
  const [closed, setClosed] = useState('')

  const key = item.normalizedKey
  if (!key || !item.declineGreeting || !reasons.length) return null

  const reason = reasons.find((r) => r.code === code) || reasons[0]
  const sentence = reason.code === 'other' ? note.trim() : reason.sentence
  const needsNote = reason.code === 'other' && !note.trim()
  const letter = `${item.declineGreeting} ${sentence} ${closing}`
  const hasRecruiter = Boolean(item.recruiterName || item.recruiterEmail || item.threadUrl)
  const channel =
    item.channel === 'linkedin' || item.emailIsLinkedInRelay
      ? 'linkedin'
      : item.recruiterEmail
        ? 'email'
        : 'other'
  const subject = [item.company, item.title].filter(Boolean).join(' ')
  const composeUrl =
    item.recruiterEmail && !item.emailIsLinkedInRelay
      ? gmailComposeUrl(item.recruiterEmail, `RE: ${subject}`, letter)
      : null

  return (
    <div className="manage-lead-form decline-lead-form">
      <h4 className="lead-panel-title">Decline Lead</h4>
      <div className="lead-panel-fields">
          <label className="manage-lead-field">
            <span>Reason</span>
            <select
              value={code}
              onChange={(e) => {
                setCode(e.target.value)
                setClosed('')
              }}
            >
              {reasons.map((r) => (
                <option key={r.code} value={r.code}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <label className="manage-lead-field manage-lead-reason-field">
            <span>{reason.code === 'other' ? 'Reason sentence' : 'Note'}</span>
            <input
              className="manage-lead-reason"
              type="text"
              placeholder={
                reason.code === 'other'
                  ? 'Goes into the letter (required)'
                  : 'Optional — saved on the lead, not in the letter'
              }
              value={note}
              onChange={(e) => {
                setNote(e.target.value)
                setClosed('')
              }}
            />
          </label>
      </div>
          {hasRecruiter && !needsNote && <pre className="draft decline-preview">{letter}</pre>}
          <div className="actions">
            {hasRecruiter && (
              <>
                <button
                  type="button"
                  className="btn"
                  disabled={needsNote}
                  onClick={async () => {
                    await copyText(letter)
                    setCopied(true)
                    setTimeout(() => setCopied(false), 1500)
                  }}
                >
                  {copied ? 'Copied' : 'Copy letter'}
                </button>
                {item.threadUrl && (
                  <a className="btn link" href={item.threadUrl} target="_blank" rel="noreferrer">
                    Open thread
                  </a>
                )}
                {composeUrl && !needsNote && (
                  <a className="btn link" href={composeUrl} target="_blank" rel="noreferrer">
                    Recruiter email
                  </a>
                )}
                {!needsNote && (
                  <a
                    className={`btn${closed === 'sent' ? ' muted' : ''}`}
                    href={declineUrl(key, code, note, { channel, letter: true })}
                    onClick={() => {
                      setClosed('sent')
                      onClosed?.()
                    }}
                    title="After you send the letter: log it and close the lead as skipped with this reason"
                  >
                    {closed === 'sent' ? 'Closed ✓ — Regenerate to see it' : 'Sent — close lead'}
                  </a>
                )}
              </>
            )}
            {!needsNote && (
              <a
                className={`btn link muted${closed === 'skip' ? ' done' : ''}`}
                href={declineUrl(key, code, note, { letter: false })}
                onClick={() => {
                  setClosed('skip')
                  onClosed?.()
                }}
                title="Close the lead as skipped with this reason, without logging a letter"
              >
                {closed === 'skip' ? 'Skipped ✓' : 'Skip without letter'}
              </a>
            )}
          </div>
    </div>
  )
}
