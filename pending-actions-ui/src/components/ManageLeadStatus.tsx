import { useContext, useState } from 'react'
import { DeclineReasonsContext } from '../lib/declineReasons'
import { declineUrl, gmailComposeUrl, gmailSearchUrl, leadStatusUrl } from '../lib/links'

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

/** Pseudo-status: not a lead stage — routes to the decline letter flow (→ skipped). */
const DECLINE_STATUS = '__decline__'

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

/** How to reach the recruiter for a message sent from Manage Lead; only
 * Contact priority rows have one. */
export interface MessageTarget {
  channel: 'linkedin' | 'email' | 'other'
  threadUrl?: string
  /** Real recruiter inbox (not a LinkedIn relay) for Gmail compose. */
  composeTo?: string
  /** Recruiter's last email, opened in Gmail (reply from there). */
  gmailUrl?: string
  /** Gmail search terms when there's no thread, address, or message link. */
  searchQuery?: string
  subject: string
  /** False when there's no recruiter at all — Decline can only skip. */
  hasRecruiter: boolean
}

/** Lets Manage Lead offer "Decline — with message" (job_tracker.pipeline.decline). */
export interface DeclineTarget {
  greeting: string
  closing: string
  /** Called after Sent / Skip is clicked (the helper app does the write). */
  onClosed?: () => void
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
 * (Contact priority renders it full-width under the row, with a message
 * target so "Follow up" and "Decline" can draft, edit, and log a message). */
export function ManageLeadForm({
  normalizedKey,
  currentStatus,
  onApplied,
  onCancel,
  message,
  followupDraft,
  decline,
  initialStatus,
}: Props & {
  normalizedKey: string
  message?: MessageTarget
  followupDraft?: string
  decline?: DeclineTarget
  /** Preselected status (e.g. Follow up on a card whose follow-up is due). */
  initialStatus?: string
}) {
  const reasons = useContext(DeclineReasonsContext)
  const startStatus = initialStatus || currentStatus || 'applied'
  const [status, setStatus] = useState(startStatus)
  const [reasonCode, setReasonCode] = useState(reasons[0]?.code || 'other')
  const [customReason, setCustomReason] = useState('')
  const [followupText, setFollowupText] = useState(followupDraft || '')
  const [letterEdit, setLetterEdit] = useState<string | null>(null)
  const [sent, setSent] = useState('')
  const [copied, setCopied] = useState(false)

  const canDecline = Boolean(decline && message && reasons.length)
  const declining = canDecline && status === DECLINE_STATUS
  const canFollowUp = Boolean(followupDraft && message)
  const followingUp = canFollowUp && status === 'following_up'

  const reason = reasons.find((r) => r.code === reasonCode) || reasons[0]
  const isOther = reason?.code === 'other'
  const needsCustom = declining && isOther && !customReason.trim()
  const generatedLetter = decline
    ? `${decline.greeting} ${isOther ? customReason.trim() : reason?.sentence || ''} ${decline.closing}`
    : ''
  const letterText = letterEdit ?? generatedLetter
  const text = declining ? letterText : followupText
  const setText = declining ? setLetterEdit : setFollowupText
  const composing = (declining && message?.hasRecruiter) || followingUp

  const reset = () => {
    setStatus(startStatus)
    setReasonCode(reasons[0]?.code || 'other')
    setCustomReason('')
    setFollowupText(followupDraft || '')
    setLetterEdit(null)
    setSent('')
  }

  const markSent = (which: string, after?: () => void) => () => {
    setSent(which)
    after?.()
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
              setSent('')
            }}
          >
            {LEAD_STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.value === 'following_up' && canFollowUp ? 'Follow up — with message' : opt.label}
              </option>
            ))}
            {canDecline && <option value={DECLINE_STATUS}>Decline — with message</option>}
          </select>
        </label>
        {declining && (
          <label className="manage-lead-field">
            <span>Reason</span>
            <select
              value={reason?.code}
              onChange={(e) => {
                setReasonCode(e.target.value)
                setLetterEdit(null)
                setSent('')
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
        {declining && isOther && (
          <label className="manage-lead-field manage-lead-reason-field">
            <span>Custom reason</span>
            <input
              className="manage-lead-reason"
              type="text"
              placeholder="Required — becomes the reason sentence in the message"
              value={customReason}
              onChange={(e) => {
                setCustomReason(e.target.value)
                setLetterEdit(null)
                setSent('')
              }}
            />
          </label>
        )}
        {!declining && !followingUp && (
          <a
            className={`btn${sent ? ' muted' : ''}`}
            href={leadStatusUrl(normalizedKey, status, '')}
            onClick={markSent('apply', onApplied)}
          >
            {sent ? (onApplied ? 'Applied ✓' : 'Applied ✓ — Regenerate to see it') : 'Apply'}
          </a>
        )}
        {!declining && !followingUp && (
          <button
            type="button"
            className="btn link muted"
            onClick={() => {
              reset()
              onCancel?.()
            }}
          >
            Cancel
          </button>
        )}
      </div>

      {composing && message && !needsCustom && (
        <div className="followup-preview">
          <label className="manage-lead-field">
            <span>Message — edit before sending if you like</span>
            <textarea
              className="draft followup-edit"
              rows={8}
              value={text}
              onChange={(e) => {
                setText(e.target.value)
                setSent('')
              }}
            />
          </label>
          <div className="actions">
            <button
              type="button"
              className="btn"
              disabled={!text.trim()}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(text)
                } catch {
                  /* clipboard can be blocked in the embedded browser; the text is selectable */
                }
                setCopied(true)
                setTimeout(() => setCopied(false), 1500)
              }}
            >
              {copied ? 'Copied' : 'Copy message'}
            </button>
            <ContactLinks target={message} text={text} />
            {text.trim() && declining && reason && (
              <a
                className={`btn${sent === 'decline' ? ' muted' : ''}`}
                href={declineUrl(normalizedKey, reason.code, isOther ? customReason : '', {
                  channel: message.channel,
                  letter: true,
                  letterText: text,
                })}
                onClick={markSent('decline', decline?.onClosed)}
                title="After you send it: log the message and close the lead as skipped with this reason"
              >
                {sent === 'decline' ? 'Message sent ✓' : 'Message sent'}
              </a>
            )}
            {text.trim() && followingUp && (
              <a
                className={`btn${sent === 'followup' ? ' muted' : ''}`}
                href={leadStatusUrl(normalizedKey, 'following_up', '', {
                  channel: message.channel,
                  message: text,
                })}
                onClick={markSent('followup', onApplied)}
                title="After you send it: log the message, set Following up, and restart the wait clock"
              >
                {sent === 'followup' ? 'Message sent ✓' : 'Message sent'}
              </a>
            )}
          </div>
        </div>
      )}

      {declining && reason && !needsCustom && (
        <div className="actions">
          <a
            className={`btn link muted${sent === 'skip' ? ' done' : ''}`}
            href={declineUrl(normalizedKey, reason.code, isOther ? customReason : '', { letter: false })}
            onClick={markSent('skip', decline?.onClosed)}
            title="Close the lead as skipped with this reason, without logging a message"
          >
            {sent === 'skip' ? 'Skipped ✓' : 'Skip without message'}
          </a>
        </div>
      )}
    </div>
  )
}

/** Thread / email / search links for reaching the recruiter, most direct first. */
function ContactLinks({ target, text }: { target: MessageTarget; text: string }) {
  return (
    <>
      {target.threadUrl && (
        <a className="btn link" href={target.threadUrl} target="_blank" rel="noreferrer">
          {/linkedin\.com/i.test(target.threadUrl) ? 'Open LinkedIn thread' : 'Open thread'}
        </a>
      )}
      {target.gmailUrl && (
        <a
          className="btn link"
          href={target.gmailUrl}
          target="_blank"
          rel="noreferrer"
          title="The recruiter's last email — reply to it there"
        >
          Open email thread
        </a>
      )}
      {target.composeTo && text.trim() && (
        <a
          className="btn link"
          href={gmailComposeUrl(target.composeTo, target.subject, text)}
          target="_blank"
          rel="noreferrer"
          title={`New Gmail draft to ${target.composeTo} with this message`}
        >
          New email to recruiter
        </a>
      )}
      {!target.threadUrl && !target.gmailUrl && !target.composeTo && target.searchQuery && (
        <a className="btn link" href={gmailSearchUrl(target.searchQuery)} target="_blank" rel="noreferrer">
          Search Gmail for the thread
        </a>
      )}
    </>
  )
}
