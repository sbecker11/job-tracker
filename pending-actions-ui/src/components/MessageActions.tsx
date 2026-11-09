import { useState } from 'react'
import { checkMessage, type QualityContext, type QualityIssue } from '../lib/replyQuality'
import type { SendRoute } from '../lib/links'

/** The one button row under every editable message, in the order of the task:
 * Copy message → Send message → Message sent? → Package sent? (when a package is ready). */
export interface MessageActionsProps {
  text: string
  /** "Apply fix" in the quality check writes the fixed text back. */
  onTextChange?: (text: string) => void
  quality: QualityContext
  route?: SendRoute
  /** Used instead of route.href when the route sends and logs itself (SendText). */
  selfLogHref?: string
  /** Logs the exact text; omit when onMessageSent does the logging (e.g. a Sent scan). */
  messageSentHref?: string
  onMessageSent?: () => void
  /** Already logged elsewhere (e.g. an earlier Sent scan for this inbound). */
  messageSentDone?: boolean
  messageSentBusy?: boolean
  /** mps://mark — shown only when the lead's résumé package is ready and not yet marked sent. */
  packageSentHref?: string
  onPackageSent?: () => void
  onSendOpened?: () => void
}

function timeNow(): string {
  return new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    /* clipboard can be blocked in the embedded browser; the text is selectable */
  }
}

export function MessageActions({
  text,
  onTextChange,
  quality,
  route,
  selfLogHref,
  messageSentHref,
  onMessageSent,
  messageSentDone = false,
  messageSentBusy = false,
  packageSentHref,
  onPackageSent,
  onSendOpened,
}: MessageActionsProps) {
  const [copiedText, setCopiedText] = useState<string | null>(null)
  const [review, setReview] = useState<QualityIssue[] | null>(null)
  const [sentAt, setSentAt] = useState('')
  const [packageAt, setPackageAt] = useState('')
  const hasText = Boolean(text.trim())
  const copied = hasText && copiedText === text
  const messageSent = messageSentDone || Boolean(sentAt)

  const doCopy = async (body: string) => {
    await copyText(body)
    setCopiedText(body)
  }
  const checkThenCopy = async () => {
    const issues = checkMessage(text, quality)
    setReview(issues)
    if (!issues.length) await doCopy(text)
  }
  const applyFix = (issue: QualityIssue) => {
    if (!issue.fix || !onTextChange) return
    const next = issue.fix(text)
    onTextChange(next)
    setReview(checkMessage(next, quality))
  }
  const logMessage = () => {
    setSentAt(timeNow())
    onMessageSent?.()
  }

  const sendHref = route ? (route.sendsAndLogs ? selfLogHref || '' : route.href(text)) : ''
  const messageSentLabel = messageSentBusy ? 'Logging…' : messageSent ? 'Message sent ✓' : 'Message sent?'

  return (
    <div className="message-actions">
      <div className="actions">
        <button
          type="button"
          className={copied ? 'btn' : 'btn primary'}
          disabled={!hasText}
          title="Checks the message first, then copies it"
          onClick={() => void checkThenCopy()}
        >
          {copied ? 'Copied ✓' : 'Copy message'}
        </button>
        {route && sendHref ? (
          copied ? (
            <a
              className="btn"
              href={sendHref}
              target={route.sendsAndLogs ? undefined : '_blank'}
              rel="noreferrer"
              title={route.step}
              onClick={route.sendsAndLogs ? logMessage : onSendOpened}
            >
              Send message
            </a>
          ) : (
            <span className="btn disabled" title="Copy the message first">
              Send message
            </span>
          )
        ) : (
          <span className="btn disabled" title="No email, LinkedIn or phone found">
            Send message
          </span>
        )}
        {messageSentHref ? (
          <a
            className={messageSent ? 'btn muted' : 'btn'}
            href={messageSentHref}
            onClick={logMessage}
            title="After you send it: log the message and start the wait clock"
          >
            {messageSentLabel}
          </a>
        ) : (
          <button
            type="button"
            className={messageSent ? 'btn muted' : 'btn'}
            disabled={!onMessageSent || messageSent || messageSentBusy}
            onClick={logMessage}
            title="After you send it: log the message and start the wait clock"
          >
            {messageSentLabel}
          </button>
        )}
        {packageSentHref && (
          <a
            className={packageAt ? 'btn muted' : 'btn'}
            href={packageSentHref}
            onClick={() => {
              setPackageAt(timeNow())
              onPackageSent?.()
            }}
            title="After the résumé (and cover letter) went out with the message: mark the package sent"
          >
            {packageAt ? 'Package sent ✓' : 'Package sent?'}
          </a>
        )}
        {route && (
          <span className="send-how">
            {route.note ?? (route.prefilled ? `opens ${route.how}, filled in` : `opens ${route.how}`)}
          </span>
        )}
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
                {i.fix && onTextChange && (
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
      {(sentAt || packageAt) && (
        <p className="send-status ok">
          {sentAt ? `Message sent ✓ — logged at ${sentAt}.` : ''}
          {sentAt && packageAt ? ' ' : ''}
          {packageAt ? `Package sent ✓ — marked at ${packageAt}.` : ''}
        </p>
      )}
    </div>
  )
}
