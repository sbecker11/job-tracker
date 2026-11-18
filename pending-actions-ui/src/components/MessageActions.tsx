import { useEffect, useRef, useState } from 'react'
import { copyMessageText, sameMessage, textFromCopyTarget } from '../lib/copyMessage'
import { isClientOnServer, openLocalUrl, revealFolderUrl, type SendRoute } from '../lib/links'
import { checkMessage, type QualityContext, type QualityIssue } from '../lib/replyQuality'

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
  /** Role folder that holds the résumé and cover letter, when the package is on disk. */
  packageFolder?: string
  onSendOpened?: () => void
}

function timeNow(): string {
  return new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

async function showPackageFolder(folder: string): Promise<string> {
  if (isClientOnServer()) {
    try {
      const res = await fetch('/api/reveal-package', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder }),
      })
      const body = (await res.json()) as { ok?: boolean; files?: string[] }
      if (res.ok && body.files?.length) return 'Finder is showing the résumé and cover letter. Drag them onto the LinkedIn message.'
      if (res.ok) return 'Opened the lead folder. The résumé and cover letter are not both in it yet.'
    } catch {
      /* fall through to the folder link */
    }
  }
  const href = revealFolderUrl(folder)
  if (!href) return 'Could not open the lead folder.'
  openLocalUrl(href)
  return 'Opened the lead folder.'
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
  packageFolder,
  onSendOpened,
}: MessageActionsProps) {
  const [copiedText, setCopiedText] = useState<string | null>(null)
  const [copyFailed, setCopyFailed] = useState(false)
  const copiedByEvent = useRef(false)
  const copiedOnPointer = useRef(false)
  const [review, setReview] = useState<QualityIssue[] | null>(null)
  const [sentAt, setSentAt] = useState('')
  const [packageAt, setPackageAt] = useState('')
  const [packageNote, setPackageNote] = useState('')
  const hasText = Boolean(text.trim())
  const copied = hasText && copiedText === text
  const messageSent = messageSentDone || Boolean(sentAt)

  useEffect(() => {
    const mark = () => {
      copiedByEvent.current = true
      setCopyFailed(false)
      setCopiedText(text)
    }
    const onCopy = (e: ClipboardEvent) => {
      const got = textFromCopyTarget(e.target) || textFromCopyTarget(document.activeElement)
      if (sameMessage(got, text)) mark()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== 'c' || e.altKey || e.shiftKey || e.metaKey || !e.ctrlKey) return
      if (!sameMessage(textFromCopyTarget(document.activeElement), text)) return
      let ok = false
      try {
        ok = document.execCommand('copy')
      } catch {
        ok = false
      }
      if (ok) mark()
    }
    document.addEventListener('copy', onCopy, true)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('copy', onCopy, true)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [text])

  const doCopy = (body: string, anchor: HTMLElement) => {
    const ok = copyMessageText(body, anchor)
    if (ok || copiedByEvent.current) {
      copiedByEvent.current = false
      setCopyFailed(false)
      setCopiedText(body)
      return
    }
    setCopyFailed(true)
  }
  const checkThenCopy = (anchor: HTMLElement) => {
    const issues = checkMessage(text, quality)
    setReview(issues)
    if (!issues.length) doCopy(text, anchor)
  }
  const copyOnPointerDown = (anchor: HTMLElement, run: (anchor: HTMLElement) => void) => {
    copiedOnPointer.current = true
    run(anchor)
  }
  const copyOnClick = (anchor: HTMLElement, run: (anchor: HTMLElement) => void) => {
    if (copiedOnPointer.current) {
      copiedOnPointer.current = false
      return
    }
    run(anchor)
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
          title="Selects this reply and copies it, the same way as Copy on the selection"
          onMouseDown={(e) => {
            e.preventDefault()
            copyOnPointerDown(e.currentTarget, checkThenCopy)
          }}
          onClick={(e) => copyOnClick(e.currentTarget, checkThenCopy)}
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
        {packageFolder && (
          <button
            type="button"
            className="btn"
            onClick={() => void showPackageFolder(packageFolder).then(setPackageNote)}
            title="Opens Finder with this lead's résumé and cover letter selected"
          >
            Show résumé &amp; cover letter
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
      {copyFailed && !copied && (
        <p className="quality-review" role="status">
          The reply is selected. Right-click Copy, or press Command+C or Control+C. Send message turns on once that copy happens.
        </p>
      )}
      {packageNote && <p className="quality-ok">{packageNote}</p>}
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
            <button
              type="button"
              className="btn primary"
              onMouseDown={(e) => {
                e.preventDefault()
                copyOnPointerDown(e.currentTarget, checkThenCopy)
              }}
              onClick={(e) => copyOnClick(e.currentTarget, checkThenCopy)}
            >
              Check again &amp; copy
            </button>
            <button
              type="button"
              className={copied ? 'btn muted' : 'btn'}
              onMouseDown={(e) => {
                e.preventDefault()
                copyOnPointerDown(e.currentTarget, doCopy.bind(null, text))
              }}
              onClick={(e) => copyOnClick(e.currentTarget, doCopy.bind(null, text))}
            >
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
