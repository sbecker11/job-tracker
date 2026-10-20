import { useContext, useState } from 'react'
import { DeclineReasonsContext } from '../lib/declineReasons'
import { gmailComposeUrl, leadStatusUrl } from '../lib/links'

/** Mirrors job_tracker.pipeline.models.LEAD_STAGES — kept as a plain literal
 * list here (not fetched from the backend) since the set of valid statuses
 * changes rarely and this avoids a round-trip just to populate a dropdown. */
export const LEAD_STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: 'new', label: 'New' },
  { value: 'pursued', label: 'Pursued' },
  { value: 'package_generated', label: 'Package generated' },
  { value: 'applied', label: 'Applied' },
  { value: 'following_up', label: 'Following up' },
  { value: 'interviewing', label: 'Interviewing' },
  { value: 'offered', label: 'Offered' },
  { value: 'accepted', label: 'Accepted' },
  { value: 'started', label: 'Started' },
  { value: 'skipped', label: 'Closed - skipped' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'deleted', label: 'Deleted' },
  { value: 'unavailable', label: 'Unavailable' },
  { value: 'hired', label: 'Hired' },
]

interface Props {
  normalizedKey?: string
  /** Preselects the dropdown when the caller already knows the lead's
   * current status (e.g. Archived/Duplicates-skipped rows) — purely a UX
   * nicety, not required for the action itself. */
  currentStatus?: string
  /** Called when Apply is clicked (the helper app does the actual write). */
  onApplied?: () => void
  /** Called after Cancel resets the form; the caller closes it. */
  onCancel?: () => void
}

/** Where a "Following up" check-in goes; only Contact priority rows have one. */
export interface FollowupTarget {
  draft: string
  channel: 'linkedin' | 'email' | 'other'
  threadUrl?: string
  /** Real recruiter inbox (not a LinkedIn relay) for Gmail compose. */
  composeTo?: string
  subject: string
}

/** "Manage lead" — a self-service status-change control, added 2026-08-26
 * after a Thyme Care lead sat marked "skipped" despite Shawn having already
 * applied, and the only way to fix it was a hand-run SQL update. Shells out
 * (via the `leadstatus://` URL scheme, tools/set-lead-status/) to
 * `set-lead-status`, which both flips job_leads.status and appends the
 * reason to job_leads.notes — see store.append_lead_note(). Deliberately
 * one small reusable control rendered on every tab's rows (Clarify, Send
 * résumé, Wait/schedule, Decide/apply, Duplicates skipped, Archived)
 * instead of a separate full lead-editing panel, which is a much larger
 * scope that was discussed before but never actually shipped. */
export function ManageLeadStatus({ normalizedKey, currentStatus, onApplied }: Props) {
  const [open, setOpen] = useState(false)

  if (!normalizedKey) return null

  return (
    <div className="manage-lead">
      <button
        type="button"
        className="btn link muted"
        onClick={() => setOpen((o) => !o)}
      >
        {open ? 'Manage lead ▲' : 'Manage lead ▾'}
      </button>
      {open && (
        <ManageLeadForm
          normalizedKey={normalizedKey}
          currentStatus={currentStatus}
          onApplied={onApplied}
          onCancel={() => setOpen(false)}
        />
      )}
    </div>
  )
}

/** The status form on its own, for callers that lay it out themselves
 * (Contact priority renders it full-width under the row). */
export function ManageLeadForm({
  normalizedKey,
  currentStatus,
  onApplied,
  onCancel,
  followup,
}: Props & { normalizedKey: string; followup?: FollowupTarget }) {
  const reasons = useContext(DeclineReasonsContext)
  const [status, setStatus] = useState(currentStatus || 'applied')
  const [reason, setReason] = useState('')
  const [reasonCode, setReasonCode] = useState(reasons[0]?.code || 'other')
  const [sent, setSent] = useState(false)
  const [message, setMessage] = useState(followup?.draft || '')
  const [copied, setCopied] = useState(false)

  const showFollowup = status === 'following_up' && Boolean(followup?.draft)
  const pickReason = status === 'rejected' && reasons.length > 0
  const picked = reasons.find((r) => r.code === reasonCode) || reasons[0]
  const customReason = !pickReason || picked?.code === 'other'
  const effectiveReason = customReason ? reason : picked?.label || ''
  const missingCustom = pickReason && customReason && !reason.trim()

  const cancel = () => {
    setStatus(currentStatus || 'applied')
    setReason('')
    setReasonCode(reasons[0]?.code || 'other')
    setSent(false)
    setMessage(followup?.draft || '')
    onCancel?.()
  }

  return (
    <div className="manage-lead-form">
      <h4 className="lead-panel-title">Manage Lead</h4>
      <div className="lead-panel-fields">
        <label className="manage-lead-field">
          <span>Status</span>
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value)
              setSent(false)
            }}
          >
            {LEAD_STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        {pickReason && (
          <label className="manage-lead-field">
            <span>Reason</span>
            <select
              value={picked?.code}
              onChange={(e) => {
                setReasonCode(e.target.value)
                setSent(false)
              }}
            >
              {reasons.map((r) => (
                <option key={r.code} value={r.code}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
        )}
        {customReason && (
          <label className="manage-lead-field manage-lead-reason-field">
            <span>{pickReason ? 'Custom reason' : 'Reason'}</span>
            <input
              className="manage-lead-reason"
              type="text"
              placeholder={pickReason ? 'Required — logged as a note' : 'Optional — logged as a note'}
              value={reason}
              onChange={(e) => {
                setReason(e.target.value)
                setSent(false)
              }}
            />
          </label>
        )}
        {missingCustom ? (
          <span className="btn disabled" title="Type a custom reason first">
            Apply
          </span>
        ) : (
          <a
            className={`btn${sent ? ' muted' : ''}`}
            href={leadStatusUrl(normalizedKey, status, effectiveReason)}
            onClick={() => {
              setSent(true)
              onApplied?.()
            }}
          >
            {sent ? (onApplied ? 'Applied ✓' : 'Applied ✓ — Regenerate to see it') : 'Apply'}
          </a>
        )}
        <button type="button" className="btn link muted" onClick={cancel}>
          Cancel
        </button>
      </div>
      {showFollowup && followup && (
        <div className="followup-preview">
          <label className="manage-lead-field">
            <span>Follow-up message — edit before sending if you like</span>
            <textarea
              className="draft followup-edit"
              rows={7}
              value={message}
              onChange={(e) => {
                setMessage(e.target.value)
                setSent(false)
              }}
            />
          </label>
          <div className="actions">
            <button
              type="button"
              className="btn"
              disabled={!message.trim()}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(message)
                } catch {
                  /* clipboard can be blocked in the embedded browser; the text is selectable */
                }
                setCopied(true)
                setTimeout(() => setCopied(false), 1500)
              }}
            >
              {copied ? 'Copied' : 'Copy message'}
            </button>
            {followup.threadUrl && (
              <a className="btn link" href={followup.threadUrl} target="_blank" rel="noreferrer">
                Open thread
              </a>
            )}
            {followup.composeTo && message.trim() && (
              <a
                className="btn link"
                href={gmailComposeUrl(followup.composeTo, followup.subject, message)}
                target="_blank"
                rel="noreferrer"
              >
                Recruiter email
              </a>
            )}
            {message.trim() && (
              <a
                className={`btn${sent ? ' muted' : ''}`}
                href={leadStatusUrl(normalizedKey, 'following_up', effectiveReason, {
                  channel: followup.channel,
                  message,
                })}
                onClick={() => {
                  setSent(true)
                  onApplied?.()
                }}
                title="After you send it: log the message, set Following up, and restart the wait clock"
              >
                {sent ? 'Marked ✓' : 'Sent — mark following up'}
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
