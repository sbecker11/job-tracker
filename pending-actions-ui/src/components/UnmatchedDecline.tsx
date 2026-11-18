import { useState } from 'react'
import { declineUrl } from '../lib/links'
import type { DeclineReason } from '../types'

/** Reason picker for an unmatched pitch. Decline closes it as a skipped lead
 * with that reason and does not send a message. */
export function UnmatchedDecline({
  messageId,
  channel,
  recruiterName,
  company,
  title,
  reasons,
  onClosed,
}: {
  messageId: string
  channel?: string
  recruiterName?: string
  company?: string
  title?: string
  reasons: DeclineReason[]
  onClosed?: () => void
}) {
  const [code, setCode] = useState(reasons[0]?.code || 'location')
  const [note, setNote] = useState('')
  const reason = reasons.find((r) => r.code === code) || reasons[0]
  const needsNote = reason?.code === 'other'
  const ready = Boolean(reason) && (!needsNote || note.trim())

  return (
    <div className="unmatched-decline">
      <label className="manage-lead-field manage-lead-action">
        <span>Decline</span>
        <select value={reason?.code || ''} onChange={(e) => setCode(e.target.value)} aria-label="Decline reason">
          {reasons.map((r) => (
            <option key={r.code} value={r.code}>
              {r.label}
            </option>
          ))}
        </select>
      </label>
      {needsNote && (
        <input
          className="manage-lead-reason"
          type="text"
          placeholder="Reason (required)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          aria-label="Custom decline reason"
        />
      )}
      {ready ? (
        <a
          className="btn"
          href={declineUrl('', reason.code, needsNote ? note : '', {
            letter: false,
            messageId,
            channel: channel || 'linkedin',
            company,
            title,
            contactName: recruiterName,
          })}
          onClick={() => onClosed?.()}
          title="Close this pitch as skipped with this reason, without sending a message"
        >
          Decline
        </a>
      ) : (
        <span className="btn disabled">Decline</span>
      )}
    </div>
  )
}
