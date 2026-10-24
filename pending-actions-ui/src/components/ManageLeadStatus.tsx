import { useContext, useState } from 'react'
import { DeclineReasonsContext } from '../lib/declineReasons'
import { checkMessage, type QualityIssue } from '../lib/replyQuality'
import {
  callUrl,
  declineUrl,
  extractSenderDetails,
  leadStatusUrl,
  saveContactUrl,
  senderSearchLinks,
  sendRoutes,
  sendTextUrl,
  type MessageTarget,
  type ReplyPlan,
  type SendRoute,
  type SenderInfo,
} from '../lib/links'

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
  { value: 'reopened', label: 'Reopened' },
]

const RECORD_STATUSES = [
  'applied',
  'following_up',
  'interviewing',
  'offered',
  'accepted',
  'started',
  'rejected',
  'unavailable',
  'hired',
  'skipped',
  'deleted',
]
/** Off-ramps that Reopen can undo; New / Pursued / Package generated are
 * pipeline-owned and not offered in the menu. */
const CLOSED_STATUSES = ['skipped', 'rejected', 'deleted', 'unavailable', 'hired']

function statusLabel(value: string): string {
  return LEAD_STATUS_OPTIONS.find((o) => o.value === value)?.label || value
}

/** Pseudo-status: log a reply without changing the lead's status. */
const REPLY_STATUS = '__reply__'

/** Number badge tying a button to its numbered step on the card. */
export function stepBadge(n: number) {
  return (
    <span className="step-badge" aria-hidden="true">
      {n}
    </span>
  )
}

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

export type { MessageTarget } from '../lib/links'

/** Lets Manage Lead offer "Reply — with message" on a card where the
 * recruiter is waiting on Shawn (Clarify). */
export interface ReplyTarget {
  draft: string
  /** The recruiter's message being answered, shown under the draft. */
  original?: string
  from?: string
  /** Called after Message sent is clicked (the helper app does the write). */
  onSent?: () => void
}

/** One of the lead's contacts, with its own route and latest received messages (CARD-14). */
export interface LeadSender {
  id: number
  name: string
  role: string
  primary: boolean
  latestAt: string
  messages: { at: string; summary: string; body: string }[]
  replyPlan?: ReplyPlan
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
  reply,
  decline,
  initialStatus,
  senders,
}: Props & {
  normalizedKey: string
  message?: MessageTarget
  followupDraft?: string
  reply?: ReplyTarget
  decline?: DeclineTarget
  /** Preselected status (e.g. Follow up on a card whose follow-up is due). */
  initialStatus?: string
  /** The lead's contacts, newest writer first (CARD-14). */
  senders?: LeadSender[]
}) {
  const reasons = useContext(DeclineReasonsContext)
  const [senderId, setSenderId] = useState<number | null>(senders?.[0]?.id ?? null)
  const chosen = senders?.find((s) => s.id === senderId)
  const target: MessageTarget | undefined =
    message && chosen && !chosen.primary && chosen.replyPlan
      ? {
          plan: chosen.replyPlan,
          sender: {
            ...message.sender,
            name: chosen.name,
            email: chosen.replyPlan.recipient.email,
            phone: chosen.replyPlan.recipient.phone,
            messageText: chosen.messages[0]?.body || '',
          },
        }
      : message
  const chosenWrote = !chosen || chosen.messages.length > 0
  const canReply = Boolean(reply?.draft && target && chosenWrote)
  const startStatus = initialStatus || (canReply ? REPLY_STATUS : '')
  const [status, setStatus] = useState(startStatus)
  const [reasonCode, setReasonCode] = useState(reasons[0]?.code || 'other')
  const [customReason, setCustomReason] = useState('')
  const [followupText, setFollowupText] = useState(followupDraft || '')
  const [replyText, setReplyText] = useState(reply?.draft || '')
  const [letterEdit, setLetterEdit] = useState<string | null>(null)
  const [sent, setSent] = useState('')
  const [sentAt, setSentAt] = useState('')
  const [copied, setCopied] = useState(false)
  const [sendOpened, setSendOpened] = useState(false)
  const [failed, setFailed] = useState(false)
  const [sentVia, setSentVia] = useState('')
  const [review, setReview] = useState<QualityIssue[] | null>(null)

  const canDecline = Boolean(decline && target && reasons.length)
  const declining = canDecline && status === DECLINE_STATUS
  const canFollowUp = Boolean(followupDraft && target)
  const followingUp = canFollowUp && status === 'following_up'
  const replying = canReply && status === REPLY_STATUS

  const greetName = chosen && !chosen.primary ? firstNameOf(chosen.name) : ''
  const regreet = (t: string) => (greetName ? t.replace(/^(Hi|Hello|Dear) [^,\n]*,/, `$1 ${greetName},`) : t)
  const reason = reasons.find((r) => r.code === reasonCode) || reasons[0]
  const isOther = reason?.code === 'other'
  const needsCustom = declining && isOther && !customReason.trim()
  const generatedLetter = decline
    ? `${regreet(decline.greeting)} ${isOther ? customReason.trim() : reason?.sentence || ''} ${decline.closing}`
    : ''
  const letterText = letterEdit ?? generatedLetter
  const text = declining ? letterText : replying ? replyText : followupText
  const setText = declining ? setLetterEdit : replying ? setReplyText : setFollowupText
  const composing = (declining && target?.plan.hasRecruiter) || followingUp || replying
  const messageMode = declining || followingUp || replying
  const word = replying ? 'reply' : followingUp ? 'follow-up' : 'decline'

  const clearSend = () => {
    setReview(null)
    setSent('')
    setSentAt('')
    setCopied(false)
    setSendOpened(false)
    setFailed(false)
  }

  const reset = () => {
    setStatus(startStatus)
    setReasonCode(reasons[0]?.code || 'other')
    setCustomReason('')
    setFollowupText(followupDraft || '')
    setReplyText(reply?.draft || '')
    setLetterEdit(null)
    clearSend()
  }

  const markSent = (which: string, after?: () => void) => () => {
    setSent(which)
    setFailed(false)
    setSentAt(new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }))
    after?.()
  }

  const routes = target ? sendRoutes(target) : []
  const route = routes[0]
  const otherRoutes = routes.slice(1)
  const phone = (target?.sender.phone || '').trim()
  const mode = declining ? 'decline' : replying ? 'reply' : 'followup'
  const afterSent = declining ? decline?.onClosed : replying ? reply?.onSent : onApplied
  const textHref =
    route?.sendsAndLogs && text.trim()
      ? sendTextUrl(
          normalizedKey,
          route.to,
          target?.sender.name || '',
          text,
          declining && reason
            ? { declineReason: reason.code, declineNote: isOther ? customReason : '' }
            : { status: followingUp ? 'following_up' : currentStatus || 'new' },
        )
      : ''
  const channel = sentVia || route?.channel || target?.plan.channel || 'other'
  const logHref =
    declining && reason
      ? declineUrl(normalizedKey, reason.code, isOther ? customReason : '', { channel, letter: true, letterText: text })
      : replying
        ? leadStatusUrl(normalizedKey, currentStatus || 'new', '', { channel, message: text })
        : followingUp
          ? leadStatusUrl(normalizedKey, 'following_up', '', { channel, message: text })
          : ''
  const isSent = Boolean(sent) && sent === mode
  const qualityCtx = {
    theirMessage: (chosen ? chosen.messages[0]?.body : reply?.original) || '',
    firstName: firstNameOf(target?.sender.name || ''),
    kind: (replying ? 'reply' : followingUp ? 'follow-up' : 'decline') as 'reply' | 'follow-up' | 'decline',
  }
  const doCopy = async (body: string) => {
    await copyText(body)
    setCopied(true)
  }
  const checkThenCopy = async () => {
    const issues = checkMessage(text, qualityCtx)
    setReview(issues)
    if (!issues.length) await doCopy(text)
  }
  const applyFix = (issue: QualityIssue) => {
    if (!issue.fix) return
    const next = issue.fix(text)
    setText(next)
    setCopied(false)
    setSendOpened(false)
    setReview(checkMessage(next, qualityCtx))
  }

  const received = (
    senders?.length
      ? senders.flatMap((s) => s.messages.map((m) => ({ ...m, from: s.name })))
      : reply?.original?.trim()
        ? [{ at: '', summary: '', body: reply.original, from: reply.from || 'recruiter' }]
        : []
  )
    .sort((a, b) => (b.at || '').localeCompare(a.at || ''))
    .slice(0, 3)

  const chooseSender = (id: number) => {
    const next = senders?.find((s) => s.id === id)
    setSenderId(id)
    clearSend()
    const name = next && !next.primary ? firstNameOf(next.name) : firstNameOf(senders?.find((s) => s.primary)?.name || '')
    const swap = (t: string) => (name ? t.replace(/^(Hi|Hello|Dear) [^,\n]*,/, `$1 ${name},`) : t)
    setReplyText((t) => swap(t))
    setFollowupText((t) => swap(t))
    setLetterEdit((t) => (t == null ? t : swap(t)))
    if (next && !next.messages.length && status === REPLY_STATUS) setStatus(canFollowUp ? 'following_up' : '')
  }

  return (
    <div className="manage-lead-form">
      <h4 className="lead-panel-title">Manage Lead</h4>
      {currentStatus && (
        <p className="manage-lead-current">
          Current status: <strong>{statusLabel(currentStatus)}</strong>
        </p>
      )}

      {(received.length > 0 || (senders?.length ?? 0) > 0) && (
        <section className="manage-lead-section">
          <p className="manage-lead-section-title">Latest received messages</p>
          {received.length ? (
            received.map((m, i) => (
              <details key={`${m.at}:${i}`} className="original-message" open={i === 0}>
                <summary>
                  {m.from}
                  {m.at ? ` · ${formatWhen(m.at)}` : ''}
                  {m.summary ? ` — ${m.summary}` : ''}
                </summary>
                <pre className="draft original-message-body">{m.body}</pre>
              </details>
            ))
          ) : (
            <p className="empty-hint">No messages received on this lead yet.</p>
          )}
          {senders && senders.length > 0 && (
            <label className="manage-lead-field">
              <span>Sender/contact:</span>
              <select value={senderId ?? ''} onChange={(e) => chooseSender(Number(e.target.value))}>
                {senders.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                    {s.role && s.role !== 'recruiter' ? ` (${s.role.replace('_', ' ')})` : ''}
                    {s.latestAt ? ` — last wrote ${formatWhen(s.latestAt)}` : ' — no messages yet'}
                  </option>
                ))}
              </select>
            </label>
          )}
        </section>
      )}

      <section className="manage-lead-section">
        <div className="lead-panel-fields">
          <label className="manage-lead-field manage-lead-action">
            <span>Take action:</span>
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                clearSend()
              }}
            >
              <option value="">Choose…</option>
              {currentStatus && CLOSED_STATUSES.includes(currentStatus) && (
                <optgroup label="Undo">
                  <option value="reopened">Reopen</option>
                </optgroup>
              )}
              {(canReply || canFollowUp || canDecline) && (
                <optgroup label="Send a message">
                  {canReply && <option value={REPLY_STATUS}>Reply — with message</option>}
                  {canFollowUp && <option value="following_up">Follow up — with message</option>}
                  {canDecline && <option value={DECLINE_STATUS}>Decline — with message</option>}
                </optgroup>
              )}
              <optgroup label="Record what happened">
                {RECORD_STATUSES.filter((v) => v !== currentStatus && !(v === 'following_up' && canFollowUp)).map(
                  (v) => (
                    <option key={v} value={v}>
                      {statusLabel(v)}
                    </option>
                  ),
                )}
              </optgroup>
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
                  clearSend()
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
                  clearSend()
                }}
              />
            </label>
          )}
          {!messageMode && !status && (
            <span className="btn disabled" title="Choose a status first">
              Apply
            </span>
          )}
          {!messageMode && status && (
            <a
              className={`btn${sent ? ' muted' : ''}`}
              href={leadStatusUrl(normalizedKey, status, '')}
              onClick={markSent('apply', onApplied)}
            >
              {sent ? (onApplied ? 'Applied ✓' : 'Applied ✓ — Regenerate to see it') : 'Apply'}
            </a>
          )}
          {!messageMode && (
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

        {composing && target && !needsCustom && (
          <div className="followup-preview">
            <label className="manage-lead-field">
              <span>Your {word} message — edit before sending if you like</span>
              <textarea
                className="draft followup-edit"
                rows={8}
                value={text}
                onChange={(e) => {
                  setText(e.target.value)
                  clearSend()
                }}
              />
            </label>
            <div className="actions">
              <button
                type="button"
                className={copied ? 'btn' : 'btn primary'}
                disabled={!text.trim()}
                title="Checks the message first, then copies it"
                onClick={() => void checkThenCopy()}
              >
                {stepBadge(2)}
                {copied ? `Copied ${word} message ✓` : `Copy ${word} message`}
              </button>
              {route ? (
                copied ? (
                  <a
                    className={sendOpened || isSent ? 'btn' : 'btn primary'}
                    href={route.sendsAndLogs ? textHref : route.href(text)}
                    target={route.sendsAndLogs ? undefined : '_blank'}
                    rel="noreferrer"
                    title={route.step}
                    onClick={
                      route.sendsAndLogs
                        ? markSent(mode, afterSent)
                        : () => {
                            setSentVia('')
                            setSendOpened(true)
                            setFailed(false)
                          }
                    }
                  >
                    {stepBadge(3)}
                    Send {word} message
                  </a>
                ) : (
                  <span className="btn disabled" title="Copy the message first">
                    {stepBadge(3)}
                    Send {word} message
                  </span>
                )
              ) : (
                <span className="btn disabled">No way to send found — see More options</span>
              )}
              {route && <span className="send-how">{route.prefilled ? `opens ${route.how}, filled in` : `opens ${route.how}`}</span>}
            </div>
            {review && review.length === 0 && copied && <p className="quality-ok">Quality check passed ✓ — copied.</p>}
            {review && review.length > 0 && (
              <div className="quality-review" role="status">
                <p className="quality-title">
                  Quality check — {review.length} {review.length === 1 ? 'suggestion' : 'suggestions'} before you copy
                </p>
                <ul>
                  {review.map((i) => (
                    <li key={i.id} className={`quality-item ${i.level}`}>
                      <span className="quality-level">{i.level === 'rule' ? 'House rule' : 'Suggestion'}</span>{' '}
                      {i.message} <span className="quality-suggestion">{i.suggestion}</span>
                      {i.fix && (
                        <button type="button" className="btn link" onClick={() => applyFix(i)}>
                          Apply fix
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
                <div className="actions">
                  <button type="button" className="btn primary" onClick={() => void checkThenCopy()}>
                    Check again &amp; copy
                  </button>
                  <button type="button" className={copied ? 'btn muted' : 'btn'} onClick={() => void doCopy(text)}>
                    {copied ? 'Copied anyway ✓' : 'Copy anyway'}
                  </button>
                </div>
              </div>
            )}
            {sendOpened && !isSent && !route?.sendsAndLogs && (
              <div className="actions send-confirm">
                <span>Did it send?</span>
                <a className="btn primary" href={logHref} onClick={markSent(mode, afterSent)} title="Log the message">
                  {stepBadge(4)}
                  Yes, it sent
                </a>
                <button type="button" className="btn" onClick={() => setFailed(true)}>
                  No
                </button>
                <label className="sent-via" title="Which app you actually sent it from — logged with the message">
                  via{' '}
                  <select value={channel} onChange={(e) => setSentVia(e.target.value)}>
                    {SENT_VIA.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            {(isSent || failed) && (
              <p className={`send-status ${isSent ? 'ok' : 'failed'}`}>
                {isSent
                  ? `Sent ✓ — logged${sentAt ? ` at ${sentAt}` : ''}.`
                  : 'Not sent. Fix the message or pick another way under More options, then copy and send again.'}
              </p>
            )}
            <details className="more-options">
              <summary className="btn link">More options</summary>
              <SendToBox route={route} message={target} />
              <div className="actions secondary-actions">
                {otherRoutes.map((r) => (
                  <a
                    key={r.button}
                    className="btn link"
                    href={r.href(text)}
                    target="_blank"
                    rel="noreferrer"
                    onClick={() => {
                      setSentVia(r.channel)
                      setSendOpened(true)
                      if (!r.prefilled) void copyText(text)
                    }}
                  >
                    {r.button}
                  </a>
                ))}
                {phone && !routes.some((r) => r.how === 'call') && (
                  <a className="btn link" href={callUrl(phone)} title="Call instead — the message works as talking points">
                    Call {target.sender.name?.split(/\s+/)[0] || phone}
                  </a>
                )}
                <SenderSearch sender={target.sender} normalizedKey={normalizedKey} onSaved={onApplied} />
              </div>
            </details>
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
      </section>
    </div>
  )
}

function firstNameOf(name: string): string {
  const n = (name || '').trim()
  return n && !n.includes('@') ? n.split(/\s+/)[0] : ''
}

function formatWhen(at: string): string {
  const d = new Date(at)
  return Number.isNaN(d.getTime())
    ? at
    : d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

const SENT_VIA: [string, string][] = [
  ['linkedin', 'LinkedIn'],
  ['email', 'Email'],
  ['sms', 'Text'],
  ['other', 'Other'],
]

function CopyField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="send-to-row">
      <span className="send-to-label">{label}</span>
      <span className="send-to-value">{value}</span>
      <button
        type="button"
        className="btn link"
        onClick={async () => {
          await copyText(value)
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

/** Who the message goes to and how — copyable, so a route that can't be
 * pre-filled (or no route at all) is still a straight copy/paste. */
function SendToBox({ route, message }: { route?: SendRoute; message: MessageTarget }) {
  const name = (message.sender.name || '').trim()
  const { via, email } = message.plan.recipient
  const viaNote = via && route?.to === email ? ` (found via ${via})` : ''
  return (
    <div className="send-to">
      <p className="send-to-title">
        Send to{route ? ` — ${route.how}${viaNote}` : ' — no email, LinkedIn or phone found; copy these into your email or LinkedIn'}
      </p>
      {name && <CopyField label="Name" value={name} />}
      {route && route.to !== name && <CopyField label={route.how === 'text' ? 'Phone' : 'To'} value={route.to} />}
      {(!route || route.channel === 'email') && message.plan.subject && (
        <CopyField label="Subject" value={message.plan.subject} />
      )}
    </div>
  )
}

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    /* clipboard can be blocked in the embedded browser; the text is selectable */
  }
}

const CONTACT_ROLES = [
  { value: 'recruiter', label: 'Recruiter' },
  { value: 'company', label: 'Company (careers / HR)' },
  { value: 'hiring_manager', label: 'Hiring manager' },
]

/** Search Gmail / LinkedIn / the web for the sender, then save what was found
 * (pre-filled from their message) so the next regenerate picks a direct route. */
function SenderSearch({
  sender,
  normalizedKey,
  onSaved,
}: {
  sender: SenderInfo
  normalizedKey: string
  onSaved?: () => void
}) {
  const links = senderSearchLinks(sender)
  const fromMessage = extractSenderDetails(sender.messageText || '')
  const [found, setFound] = useState({
    name: sender.name || '',
    email: sender.email ? '' : fromMessage.email,
    phone: sender.phone ? '' : fromMessage.phone,
    linkedin: fromMessage.linkedin,
    role: 'recruiter',
  })
  const [saved, setSaved] = useState(false)
  const set = (k: keyof typeof found) => (e: { target: { value: string } }) => {
    setFound((f) => ({ ...f, [k]: e.target.value }))
    setSaved(false)
  }
  const hasDetail = Boolean(found.email.trim() || found.phone.trim() || found.linkedin.trim())
  const prefilled = Boolean(
    (fromMessage.email && !sender.email) || (fromMessage.phone && !sender.phone) || fromMessage.linkedin,
  )
  return (
    <details className="sender-search">
      <summary className="btn link">
        {prefilled
          ? 'Wrong or missing recipient? Search for sender (details found in their message)'
          : 'Wrong or missing recipient? Search for sender'}
      </summary>
      {links.length > 0 && (
        <div className="sender-search-links">
          {links.map((l) => (
            <a key={l.label} className="btn link" href={l.href} target="_blank" rel="noreferrer">
              {l.label}
            </a>
          ))}
        </div>
      )}
      <div className="lead-panel-fields save-contact">
        <label className="manage-lead-field">
          <span>Name</span>
          <input className="manage-lead-reason" value={found.name} onChange={set('name')} />
        </label>
        <label className="manage-lead-field">
          <span>Email</span>
          <input className="manage-lead-reason" type="email" value={found.email} onChange={set('email')} />
        </label>
        <label className="manage-lead-field">
          <span>LinkedIn profile URL</span>
          <input className="manage-lead-reason" value={found.linkedin} onChange={set('linkedin')} />
        </label>
        <label className="manage-lead-field">
          <span>Phone</span>
          <input className="manage-lead-reason" value={found.phone} onChange={set('phone')} />
        </label>
        <label className="manage-lead-field">
          <span>This is</span>
          <select value={found.role} onChange={set('role')}>
            {CONTACT_ROLES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        {hasDetail ? (
          <a
            className={`btn${saved ? ' muted' : ''}`}
            href={saveContactUrl(normalizedKey, found)}
            onClick={() => {
              setSaved(true)
              onSaved?.()
            }}
            title="Save to this lead; the page then picks the best way to send"
          >
            {saved ? 'Saved ✓' : 'Save contact'}
          </a>
        ) : (
          <span className="btn disabled" title="Enter an email, LinkedIn URL, or phone first">
            Save contact
          </span>
        )}
      </div>
    </details>
  )
}
